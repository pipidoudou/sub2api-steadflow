package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/paymentorder"
	"github.com/Wei-Shaw/sub2api/internal/payment"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const PaymentAuditActionManualFulfillmentConfirmed = "MANUAL_FULFILLMENT_CONFIRMED"

// ConfirmManualSubscriptionFulfillment records a subscription delivery that an
// administrator already completed outside this system. It intentionally avoids
// the automated fulfillment path and all of its subscription, rebate, provider,
// and notification side effects.
func (s *PaymentService) ConfirmManualSubscriptionFulfillment(ctx context.Context, orderID int64, operator string) (*dbent.PaymentOrder, error) {
	tx, err := s.entClient.Tx(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin manual fulfillment transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	current, err := tx.PaymentOrder.Get(ctx, orderID)
	if err != nil {
		if dbent.IsNotFound(err) {
			return nil, infraerrors.NotFound("NOT_FOUND", "order not found")
		}
		return nil, fmt.Errorf("get manual fulfillment order: %w", err)
	}
	if current.OrderType != payment.OrderTypeSubscription {
		return nil, infraerrors.BadRequest("INVALID_ORDER_TYPE", "only subscription orders can be marked manually fulfilled")
	}
	if current.Status != OrderStatusPaid {
		return nil, infraerrors.Conflict("CONFLICT", "order is no longer awaiting fulfillment")
	}

	now := time.Now()
	updatedCount, err := tx.PaymentOrder.Update().Where(
		paymentorder.IDEQ(orderID),
		paymentorder.OrderTypeEQ(payment.OrderTypeSubscription),
		paymentorder.StatusEQ(OrderStatusPaid),
	).
		SetStatus(OrderStatusCompleted).
		SetCompletedAt(now).
		SetUpdatedAt(now).
		Save(ctx)
	if err != nil {
		return nil, fmt.Errorf("mark order manually fulfilled: %w", err)
	}
	if updatedCount != 1 {
		return nil, infraerrors.Conflict("CONFLICT", "order status changed; refresh and retry")
	}

	detail, err := json.Marshal(map[string]any{
		"previous_status":                 OrderStatusPaid,
		"next_status":                     OrderStatusCompleted,
		"delivery_mode":                   "manual_external",
		"automatic_subscription_assigned": false,
		"affiliate_rebate_issued":         false,
		"provider_called":                 false,
		"notification_email_sent":         false,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal manual fulfillment audit: %w", err)
	}
	if strings.TrimSpace(operator) == "" {
		operator = "admin"
	}
	if _, err := tx.PaymentAuditLog.Create().
		SetOrderID(strconv.FormatInt(orderID, 10)).
		SetAction(PaymentAuditActionManualFulfillmentConfirmed).
		SetDetail(string(detail)).
		SetOperator(operator).
		Save(ctx); err != nil {
		return nil, fmt.Errorf("write manual fulfillment audit: %w", err)
	}

	updated, err := tx.PaymentOrder.Get(ctx, orderID)
	if err != nil {
		return nil, fmt.Errorf("reload manually fulfilled order: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit manual fulfillment transaction: %w", err)
	}
	return updated, nil
}
