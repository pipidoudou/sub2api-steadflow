// Package distributor - distributor_order_service.go
//
// 编排 EnsureOrderUser + 创建分销商订单 + IssueApiKey + 订阅履行（v2.2 §4.3.3）
//
// 关于 PaymentService.CreateOrder（v2.2 §4.3.4 决策记录）：
//
//	真实 PaymentService.CreateOrder(ctx, service.CreateOrderRequest) 会触发微信/支付宝
//	等三方支付通道（payment_order.invoice_url/二维码）。分销商场景下，BFF（站新）是
//	真正的收款方，console 仅作为订单台账——因此**不能**调用 PaymentService.CreateOrder。
//	本服务通过 ent.PaymentOrder.Create() 直接落台账，由 BFF webhook 异步驱动履行。
//	这样可以保持 console 现有 PaymentService 不被改动（P0-2 修复策略）。
package distributor

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/paymentorder"
)

// CreateDistributorOrderRequest 是站新 BFF → console 的请求。
type CreateDistributorOrderRequest struct {
	ExternalOrderID string  `json:"externalOrderId"`         // 站新侧订单号（用于幂等）
	Email           string  `json:"email"`                   // 用户邮箱（用于 shadow user 兜底创建）
	Service         string  `json:"service"`                 // codex / account / recharge
	Tier            string  `json:"tier"`                    // site 侧套餐编码，仅作 fallback / 审计
	GroupID         int64   `json:"groupId,omitempty"`       // console 侧订阅/账号分组 ID
	PlanID          int64   `json:"planId,omitempty"`        // console 侧套餐/计划 ID
	ValidityDays    int     `json:"validityDays,omitempty"`  // 有效期天数
	ClientIP        string  `json:"clientIp,omitempty"`      // 下单来源 IP，供台账审计
	Amount          float64 `json:"amount"`                  // 金额（元）
	NotifyURL       string  `json:"notifyUrl,omitempty"`     // 订阅履行后回调（BFF 接口）
	WebhookSecret   string  `json:"webhookSecret,omitempty"` // 发送回 BFF 时的 HMAC 共享密钥；缺省时跳过签名（仅 dev）
	DistributorID   int64   `json:"-"`                       // 鉴权绑定的内部 distributor_bindings.id
}

// CreateDistributorOrderResponse 是 console → BFF 的响应。
type CreateDistributorOrderResponse struct {
	SupplierOrderID string `json:"supplier_order_id"`
	Status          string `json:"status"` // "processing"
	ShadowUserID    int64  `json:"shadow_user_id"`
	APIKeyPreview   string `json:"api_key_preview,omitempty"`
	APIKey          string `json:"api_key,omitempty"`
	ExpiresAt       string `json:"expires_at,omitempty"`
}

// PlanLookupFunc 解析套餐 → planID / groupID / days 的可注入函数（便于单测）。
type PlanLookupFunc func(ctx context.Context, tier string) (planID, groupID int64, days int, err error)

// SubscriptionAssigner 是真实 SubscriptionService.AssignOrExtendSubscription 的接口（解耦）。
type SubscriptionAssigner interface {
	AssignOrExtendSubscription(ctx context.Context, input *SubscriptionAssignInput) error
}

// SubscriptionAssignInput 是 v2.2 §4.3.3 的订阅分配输入。
// 与 service.AssignSubscriptionInput 字段一致（UserID / GroupID / ValidityDays / AssignedBy / Notes）。
type SubscriptionAssignInput struct {
	UserID       int64
	GroupID      int64
	ValidityDays int
	AssignedBy   int64
	Notes        string
}

// DistributorOrderService 编排分销商订单创建。
type DistributorOrderService struct {
	db                  *dbent.Client
	shadowService       *ShadowUserService
	apiKeyService       *UserAPIKeyService
	subscriptionService SubscriptionAssigner
	httpClient          *http.Client
	logger              *slog.Logger
	resolvePlan         PlanLookupFunc
}

// NewDistributorOrderService 构造 DistributorOrderService。
func NewDistributorOrderService(
	db *dbent.Client,
	shadowService *ShadowUserService,
	apiKeyService *UserAPIKeyService,
	subscriptionService SubscriptionAssigner,
	logger *slog.Logger,
) *DistributorOrderService {
	if logger == nil {
		logger = slog.Default()
	}
	return &DistributorOrderService{
		db:                  db,
		shadowService:       shadowService,
		apiKeyService:       apiKeyService,
		subscriptionService: subscriptionService,
		httpClient:          &http.Client{Timeout: 5 * time.Second},
		logger:              logger,
		resolvePlan:         defaultResolveCodexPlan,
	}
}

// SetResolvePlan 注入套餐解析函数（用于测试）。
func (s *DistributorOrderService) SetResolvePlan(fn PlanLookupFunc) {
	if fn != nil {
		s.resolvePlan = fn
	}
}

// CreateOrder 创建 console 侧订单 + 兜底 shadow user + 颁发 API key。
func (s *DistributorOrderService) CreateOrder(ctx context.Context, req CreateDistributorOrderRequest) (*CreateDistributorOrderResponse, error) {
	if req.Email == "" {
		return nil, errors.New("email required")
	}
	if req.Service != "codex" {
		// 当前轮只实现 codex 自动化，account/recharge 由客服手工
		return nil, fmt.Errorf("service %s not supported in this endpoint", req.Service)
	}
	if strings.TrimSpace(req.ExternalOrderID) == "" {
		return nil, errors.New("externalOrderId required")
	}

	// BFF 重试、支付通知重放和网络超时重试都必须复用同一笔台账。
	existing, err := s.db.PaymentOrder.Query().
		Where(paymentorder.OutTradeNoEQ(req.ExternalOrderID)).
		Only(ctx)
	if err == nil {
		return s.resumeExistingOrder(ctx, existing, req)
	}
	if !dbent.IsNotFound(err) {
		return nil, fmt.Errorf("query existing ledger order: %w", err)
	}

	// 1. 兜底创建 shadow user（幂等）
	userID, isNew, err := s.shadowService.EnsureOrderUser(ctx, req.Email)
	if err != nil {
		return nil, fmt.Errorf("ensure shadow user: %w", err)
	}
	s.logger.Info("distributor order: shadow user ensured",
		"user_id", userID,
		"is_newly_created", isNew,
		"external_order_id", req.ExternalOrderID,
	)

	// 2. 解析套餐 → planID / groupID / days。site 显式传入的 console 映射优先，
	// tier fallback 仅保留给旧调用和单测。
	planID, groupID, days, err := s.resolveOrderPlan(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("resolve plan: %w", err)
	}

	// 3. 直接落 payment_orders 台账（避免触发 PaymentService.CreateOrder 的三方支付通道）
	orderID, err := s.createOrderInLedger(ctx, userID, req, planID, groupID, days)
	if err != nil {
		// 并发重放可能在两次 SELECT 之间命中 out_trade_no 唯一约束；
		// 重新读取已存在订单，仍走同一套复用/补订阅逻辑。
		if existing, lookupErr := s.db.PaymentOrder.Query().
			Where(paymentorder.OutTradeNoEQ(req.ExternalOrderID)).
			Only(ctx); lookupErr == nil {
			return s.resumeExistingOrder(ctx, existing, req)
		}
		return nil, fmt.Errorf("create ledger order: %w", err)
	}
	s.logger.Info("distributor order: payment order created (ledger)",
		"order_id", orderID,
		"user_id", userID,
	)

	// 4. 颁发 API key
	apiKey, err := s.apiKeyService.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		// API key 颁发失败时保留可恢复的台账状态，后续同一 external order
		// 可以补签发，而不是被幂等查询永久卡住。
		failureReason := "api key issuance failed: " + err.Error()
		if _, updateErr := s.db.PaymentOrder.UpdateOneID(orderID).
			SetStatus("processing").SetFailedReason(failureReason).Save(ctx); updateErr != nil {
			return nil, fmt.Errorf("issue api key (%v); persist retry state failed: %w", err, updateErr)
		}
		return nil, fmt.Errorf("issue api key: %w", err)
	}

	// 5. 触发订阅（自动履行）。API key 已颁发后，订阅分配失败不能让分销商订单整体失败；
	//    否则 BFF 收不到 api_key，会把已生成 key 的订单标记为失败。
	if err := s.subscriptionService.AssignOrExtendSubscription(ctx, &SubscriptionAssignInput{
		UserID:       userID,
		GroupID:      groupID,
		ValidityDays: days,
		AssignedBy:   userID, // shadow user = 自动履行；后续人工可改
		Notes:        fmt.Sprintf("distributor: %s", req.ExternalOrderID),
	}); err != nil {
		logger := s.logger
		if logger == nil {
			logger = slog.Default()
		}
		logger.Warn("distributor order: subscription assign failed after api key issued",
			"external_order_id", req.ExternalOrderID,
			"user_id", userID,
			"group_id", groupID,
			"err", err.Error(),
		)
		if _, updateErr := s.db.PaymentOrder.UpdateOneID(orderID).
			SetStatus("processing").
			SetFailedReason(err.Error()).
			Save(ctx); updateErr != nil {
			return nil, fmt.Errorf("subscription assignment failed (%v); persist retry state failed: %w", err, updateErr)
		}
		// 订阅失败时不能返回 api_key，也不能发送 fulfilled webhook。
		return nil, fmt.Errorf("subscription assignment failed: %w", err)
	}

	// 6. 触发 console webhook → BFF（HMAC-SHA256 签名，v2.2 §4.3.4）
	//    失败不影响主流程（best-effort），由 BFF 主动 poll 兜底。
	expiresAt := time.Now().Add(time.Duration(days) * 24 * time.Hour).UTC().Format(time.RFC3339Nano)
	if req.NotifyURL != "" {
		if err := s.notifyBFFWebhook(ctx, req.NotifyURL, req.WebhookSecret, orderID, userID, apiKey, expiresAt); err != nil {
			logger := s.logger
			if logger == nil {
				logger = slog.Default()
			}
			logger.Warn("distributor order: webhook notify failed (best-effort)",
				"notify_url", req.NotifyURL,
				"order_id", orderID,
				"err", err.Error(),
			)
		}
	}

	return &CreateDistributorOrderResponse{
		SupplierOrderID: fmt.Sprintf("CONSOX-%d", orderID),
		Status:          "processing",
		ShadowUserID:    userID,
		APIKeyPreview:   maskAPIKey(apiKey),
		APIKey:          apiKey,
		ExpiresAt:       expiresAt,
	}, nil
}

// resumeExistingOrder 处理同一 external order 的重试：复用已签发 key，只有明确的
// subscription failure 才补订阅，避免重放请求再次延长订阅。
func (s *DistributorOrderService) resumeExistingOrder(
	ctx context.Context,
	order *dbent.PaymentOrder,
	req CreateDistributorOrderRequest,
) (*CreateDistributorOrderResponse, error) {
	groupID := req.GroupID
	if order.SubscriptionGroupID != nil {
		groupID = *order.SubscriptionGroupID
	}
	if groupID <= 0 {
		return nil, errors.New("existing distributor order missing subscription group")
	}
	apiKey, expiresAt, err := s.apiKeyService.GetActiveApiKey(ctx, order.UserID, groupID)
	if err != nil {
		// 初次签发失败会留下 processing + failed_reason。这里补签发一次，
		// 成功后继续进入订阅恢复；不会重复创建 payment_order。
		if order.FailedReason == nil || !strings.HasPrefix(strings.TrimSpace(*order.FailedReason), "api key issuance failed:") {
			return nil, fmt.Errorf("existing distributor order api key unavailable: %w", err)
		}
		apiKey, err = s.apiKeyService.IssueApiKey(ctx, order.UserID, groupID)
		if err != nil {
			_, _ = s.db.PaymentOrder.UpdateOneID(order.ID).SetStatus("processing").SetFailedReason("api key issuance failed: " + err.Error()).Save(ctx)
			return nil, fmt.Errorf("retry api key issuance: %w", err)
		}
		expiresAtValue := time.Now().Add(30 * 24 * time.Hour)
		expiresAt = &expiresAtValue
	}

	if order.FailedReason != nil && strings.TrimSpace(*order.FailedReason) != "" {
		days := req.ValidityDays
		if order.SubscriptionDays != nil {
			days = *order.SubscriptionDays
		}
		if days <= 0 {
			return nil, errors.New("existing distributor order missing subscription days")
		}
		if err := s.subscriptionService.AssignOrExtendSubscription(ctx, &SubscriptionAssignInput{
			UserID: order.UserID, GroupID: groupID, ValidityDays: days, AssignedBy: order.UserID,
			Notes: fmt.Sprintf("distributor retry: %s", req.ExternalOrderID),
		}); err != nil {
			_, _ = s.db.PaymentOrder.UpdateOneID(order.ID).SetStatus("processing").SetFailedReason(err.Error()).Save(ctx)
			return nil, fmt.Errorf("subscription retry failed: %w", err)
		}
		if _, err := s.db.PaymentOrder.UpdateOneID(order.ID).
			SetStatus("processing").ClearFailedReason().ClearFailedAt().Save(ctx); err != nil {
			return nil, fmt.Errorf("clear subscription retry state: %w", err)
		}
	}

	if req.NotifyURL != "" {
		if err := s.notifyBFFWebhook(ctx, req.NotifyURL, req.WebhookSecret, order.ID, order.UserID, apiKey, expiresAt.UTC().Format(time.RFC3339Nano)); err != nil {
			return nil, fmt.Errorf("notify existing distributor order: %w", err)
		}
	}
	return &CreateDistributorOrderResponse{
		SupplierOrderID: fmt.Sprintf("CONSOX-%d", order.ID),
		Status:          order.Status,
		ShadowUserID:    order.UserID,
		APIKeyPreview:   maskAPIKey(apiKey),
		APIKey:          apiKey,
		ExpiresAt:       expiresAt.UTC().Format(time.RFC3339Nano),
	}, nil
}

func (s *DistributorOrderService) resolveOrderPlan(ctx context.Context, req CreateDistributorOrderRequest) (planID, groupID int64, days int, err error) {
	if req.GroupID > 0 && req.ValidityDays > 0 {
		planID = req.PlanID
		if planID <= 0 {
			planID = req.GroupID
		}
		return planID, req.GroupID, req.ValidityDays, nil
	}
	return s.resolvePlan(ctx, req.Tier)
}

// createOrderInLedger 直接通过 ent 在 payment_orders 表落台账。
//
// 关键决策（P0-2 修复记录）：
//   - 真实 PaymentService.CreateOrder 会触发微信/支付宝三方支付通道（payment_order.invoice_url / 二维码）
//   - 分销商场景下收款方是 BFF（站新），console 仅作为台账，因此跳过 PaymentService.CreateOrder
//   - 通过 ent.PaymentOrder.Create() 直接落台账，由 BFF webhook 异步驱动履行（status=processing）
//   - 这样保持 console 现有 PaymentService 完全不改
//
// 备注：payment_orders 实体的 ent 生成方法是 Create() + SetXxx(...).
// 这里仅设置 ent schema 已声明的字段（schema 未声明的 distributor_email/external_user_id/
// created_user_id/distributor_id 是通过 raw SQL ALTER 追加的，需用 ORM 会话外的 db.Exec。
// 4 个分销商扩展字段通过 UpdateOneID 在保存后追加，保持 ent 编译干净）。
func (s *DistributorOrderService) createOrderInLedger(
	ctx context.Context,
	userID int64,
	req CreateDistributorOrderRequest,
	planID, groupID int64,
	days int,
) (int64, error) {
	now := time.Now()
	subscriptionDays := days

	// 把 days 和外部订单号编码进 payment_trade_no（max 128 字符，足以容纳）；
	// out_trade_no 同步使用 external_order_id 用于 BFF webhook 回调匹配；
	// user_name 用 email（兜底，没有专门 user_name 时）；recharge_code 用
	// external_order_id 仅作为占位字段（distributor 路径不走充值码）。
	userName := req.Email
	if len(userName) > 100 {
		userName = userName[:100]
	}
	clientIP := req.ClientIP
	if clientIP == "" {
		clientIP = "127.0.0.1"
	}
	var distributorID *int64
	if req.DistributorID > 0 {
		distributorID = &req.DistributorID
	}

	created, err := s.db.PaymentOrder.Create().
		SetUserID(userID).
		SetUserEmail(req.Email).
		SetUserName(userName).
		SetAmount(req.Amount).
		SetPayAmount(req.Amount).
		SetRechargeCode(req.ExternalOrderID).
		SetOutTradeNo(req.ExternalOrderID).
		SetPaymentType("distributor").
		SetPaymentTradeNo(req.ExternalOrderID).
		SetOrderType("distributor_plan").
		SetPlanID(planID).
		SetSubscriptionGroupID(groupID).
		SetSubscriptionDays(subscriptionDays).
		SetStatus("processing").
		SetClientIP(clientIP).
		SetSrcHost("site-bff").
		SetSrcURL(req.NotifyURL).
		SetDistributorEmail(req.Email).
		SetExternalUserID(req.ExternalOrderID).
		SetCreatedUserID(userID).
		SetNillableDistributorID(distributorID).
		SetExpiresAt(now.Add(30 * time.Minute)).
		Save(ctx)
	if err != nil {
		return 0, err
	}

	return int64(created.ID), nil
}

// notifyBFFWebhook 向 BFF 发送履行完成通知（HMAC-SHA256 签名头）。
//
// v2.2 §4.3.4 要求 console → BFF webhook 必须在 header `X-Webhook-Signature` 中
// 携带 HMAC-SHA256(secret, body) 的 hex 摘要。BFF 端 verifyHmac 会在签名不匹配时返回 401。
func (s *DistributorOrderService) notifyBFFWebhook(
	ctx context.Context,
	notifyURL, webhookSecret string,
	orderID, userID int64,
	apiKey, expiresAt string,
) error {
	logger := s.logger
	if logger == nil {
		logger = slog.Default()
	}

	payload := map[string]any{
		"event":           "distributor.order.fulfilled",
		"supplierOrderId": "CONSOX-" + fmt.Sprint(orderID),
		"status":          "fulfilled",
		"shadowUserId":    userID,
		"apiKey":          apiKey,
		"expiresAt":       expiresAt,
		"timestamp":       time.Now().UTC().Format(time.RFC3339Nano),
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal webhook payload: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, notifyURL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build webhook request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if webhookSecret != "" {
		req.Header.Set("X-Webhook-Signature", SignWebhookPayload([]byte(webhookSecret), body))
	} else {
		// dev / 测试模式：不签名；BFF 端按 mockMode 路径放行
		req.Header.Set("X-Webhook-Signature", "unsigned")
		logger.Warn("distributor order: webhook sent WITHOUT HMAC signature (no secret configured)",
			"order_id", orderID,
		)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("send webhook: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("webhook status=%d", resp.StatusCode)
	}
	return nil
}

// SignWebhookPayload 计算 webhook payload 的 HMAC-SHA256 签名（导出供测试）。
// BFF verifyHmac 对应此格式：`sha256=<lowercase-hex>`。
func SignWebhookPayload(secret, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// defaultResolveCodexPlan 内置套餐映射（演示 / 单测用默认值；真实实现应查 subscription_plans 表）。
func defaultResolveCodexPlan(_ context.Context, tier string) (planID, groupID int64, days int, err error) {
	switch tier {
	case "codex_plus_30d":
		return 1, 100, 30, nil
	case "codex_pro_30d":
		return 2, 101, 30, nil
	case "codex_team_30d":
		return 3, 102, 30, nil
	default:
		return 0, 0, 0, fmt.Errorf("unknown tier: %s", tier)
	}
}

// maskAPIKey 脱敏 API key（仅展示前 6 + 后 4）。
func maskAPIKey(key string) string {
	if len(key) <= 10 {
		return "***"
	}
	return key[:6] + "***" + key[len(key)-4:]
}
