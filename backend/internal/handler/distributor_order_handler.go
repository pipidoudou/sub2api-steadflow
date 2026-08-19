// Package handler - distributor_order_handler.go（v2.2 §4.3.1）
//
// BFF 调 console 的端点：
//
//	POST /api/v1/distributor/orders       - 创建订单 + 兜底 shadow user + IssueApiKey
//	GET  /api/v1/distributor/orders/:id   - 查订单状态
//	GET  /api/v1/distributor/apikeys/:shadowUserId - 拉取真实 API key
package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/paymentorder"
	"github.com/Wei-Shaw/sub2api/internal/middleware"
	distributorSvc "github.com/Wei-Shaw/sub2api/internal/service/distributor"
)

// DistributorHandler 负责分销商端点。
type DistributorHandler struct {
	db            *dbent.Client
	orderService  *distributorSvc.DistributorOrderService
	apiKeyService *distributorSvc.UserAPIKeyService
	logger        *slog.Logger
}

// NewDistributorHandler 构造 DistributorHandler。
func NewDistributorHandler(
	db *dbent.Client,
	orderService *distributorSvc.DistributorOrderService,
	apiKeyService *distributorSvc.UserAPIKeyService,
	logger *slog.Logger,
) *DistributorHandler {
	if logger == nil {
		logger = slog.Default()
	}
	return &DistributorHandler{db: db, orderService: orderService, apiKeyService: apiKeyService, logger: logger}
}

// RegisterRoutes 注册路由（假设基于 net/http + ServeMux 或 Gin）。
func (h *DistributorHandler) RegisterRoutes(mux *http.ServeMux, authMiddleware func(http.Handler) http.Handler) {
	mux.Handle("/api/v1/distributor/orders", authMiddleware(http.HandlerFunc(h.HandleOrders)))
	mux.Handle("/api/v1/distributor/orders/", authMiddleware(http.HandlerFunc(h.HandleOrderByID)))
	mux.Handle("/api/v1/distributor/apikeys/", authMiddleware(http.HandlerFunc(h.HandleAPIKeysByUser)))
}

// HandleOrders POST /api/v1/distributor/orders
func (h *DistributorHandler) HandleOrders(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED")
		return
	}

	var req distributorSvc.CreateDistributorOrderRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_BODY")
		return
	}
	if req.Email == "" || req.Service == "" {
		writeError(w, http.StatusBadRequest, "MISSING_FIELDS")
		return
	}
	if distributorID, ok := r.Context().Value(middleware.DistributorIDKey).(int64); ok {
		req.DistributorID = distributorID
	}

	resp, err := h.orderService.CreateOrder(r.Context(), req)
	if err != nil {
		h.logger.Error("distributor create order failed",
			"err", err.Error(),
			"email", req.Email,
		)
		writeError(w, http.StatusInternalServerError, "CREATE_ORDER_FAILED")
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

// HandleOrderByID GET /api/v1/distributor/orders/:supplierOrderID
func (h *DistributorHandler) HandleOrderByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED")
		return
	}
	// 路径格式：/api/v1/distributor/orders/CONSOX-{payment_order_id}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v1/distributor/orders/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(w, http.StatusBadRequest, "MISSING_ORDER_ID")
		return
	}
	supplierOrderID := parts[0]
	distributorID, ok := distributorIDFromRequest(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "UNAUTHORIZED")
		return
	}
	orderID, err := strconv.ParseInt(strings.TrimPrefix(supplierOrderID, "CONSOX-"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_ORDER_ID")
		return
	}
	order, err := h.db.PaymentOrder.Query().
		Where(paymentorder.IDEQ(orderID), paymentorder.DistributorIDEQ(distributorID)).
		Only(r.Context())
	if err != nil {
		if dbent.IsNotFound(err) {
			writeError(w, http.StatusNotFound, "ORDER_NOT_FOUND")
			return
		}
		h.logger.Error("get distributor order failed", "err", err.Error(), "order_id", orderID)
		writeError(w, http.StatusInternalServerError, "GET_ORDER_FAILED")
		return
	}
	keys, _ := h.apiKeyService.ListByUser(r.Context(), order.UserID)
	var keyPreview string
	var expiresAt *time.Time
	for _, key := range keys {
		if key.Status == "active" && !distributorSvc.IsUserAPIKeyExpired(key) {
			keyPreview = fmt.Sprintf("sk-***%d", key.ID)
			expiresAt = key.ExpiresAt
			break
		}
	}
	response := map[string]any{
		"supplier_order_id": supplierOrderID,
		"status":            strings.ToLower(order.Status),
		"shadow_user_id":    order.UserID,
		"api_key_preview":   keyPreview,
		"expires_at":        expiresAt,
	}
	if order.FailedReason != nil && strings.TrimSpace(*order.FailedReason) != "" {
		response["retryable_error"] = *order.FailedReason
	}
	writeJSON(w, http.StatusOK, response)
}

// HandleAPIKeysByUser GET /api/v1/distributor/apikeys/:shadowUserId
func (h *DistributorHandler) HandleAPIKeysByUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED")
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/api/v1/distributor/apikeys/")
	shadowUserID, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_USER_ID")
		return
	}
	distributorID, ok := distributorIDFromRequest(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "UNAUTHORIZED")
		return
	}
	hasOrder, err := h.db.PaymentOrder.Query().
		Where(paymentorder.DistributorIDEQ(distributorID), paymentorder.UserIDEQ(shadowUserID), paymentorder.PaymentTypeEQ("distributor")).
		Exist(r.Context())
	if err != nil {
		h.logger.Error("check distributor user ownership failed", "err", err, "user_id", shadowUserID)
		writeError(w, http.StatusInternalServerError, "OWNERSHIP_CHECK_FAILED")
		return
	}
	if !hasOrder {
		writeError(w, http.StatusNotFound, "USER_NOT_FOUND")
		return
	}

	keys, err := h.apiKeyService.ListByUser(r.Context(), shadowUserID)
	if err != nil {
		h.logger.Error("list user api keys failed",
			"err", err.Error(),
			"user_id", shadowUserID,
		)
		writeError(w, http.StatusInternalServerError, "LIST_FAILED")
		return
	}

	// 仅返回第一个 active key 的 preview
	for _, k := range keys {
		if k.Status == "active" && !distributorSvc.IsUserAPIKeyExpired(k) {
			writeJSON(w, http.StatusOK, map[string]any{
				"shadow_user_id": shadowUserID,
				"status":         "active",
				"expires_at":     k.ExpiresAt,
				"key_preview":    fmt.Sprintf("sk-***%d", k.ID),
			})
			return
		}
	}

	writeJSON(w, http.StatusNotFound, map[string]any{
		"shadow_user_id": shadowUserID,
		"status":         "no_active_key",
	})
}

func distributorIDFromRequest(r *http.Request) (int64, bool) {
	id, ok := r.Context().Value(middleware.DistributorIDKey).(int64)
	return id, ok && id > 0
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]any{
		"ok":    false,
		"error": code,
	})
}

// 防止 unused imports
var _ = errors.New
var _ = paymentorder.StatusEQ
