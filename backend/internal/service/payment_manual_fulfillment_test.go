//go:build unit

package service

import (
	"context"
	"database/sql"
	"strconv"
	"testing"
	"time"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	"github.com/stretchr/testify/require"
	_ "modernc.org/sqlite"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/enttest"
	"github.com/Wei-Shaw/sub2api/ent/paymentauditlog"
	"github.com/Wei-Shaw/sub2api/internal/payment"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

func TestConfirmManualSubscriptionFulfillmentCompletesPaidSubscriptionAndAudits(t *testing.T) {
	ctx := context.Background()
	client := newPaymentManualFulfillmentTestClient(t)
	order := createPaymentManualFulfillmentOrder(t, ctx, client, payment.OrderTypeSubscription, OrderStatusPaid)
	svc := NewPaymentService(client, nil, nil, nil, nil, nil, nil, nil, nil)

	updated, err := svc.ConfirmManualSubscriptionFulfillment(ctx, order.ID, "admin:42")
	require.NoError(t, err)
	require.Equal(t, OrderStatusCompleted, updated.Status)
	require.NotNil(t, updated.CompletedAt)

	logs, err := client.PaymentAuditLog.Query().
		Where(paymentauditlog.OrderIDEQ(strconv.FormatInt(order.ID, 10))).
		All(ctx)
	require.NoError(t, err)
	require.Len(t, logs, 1)
	require.Equal(t, "MANUAL_FULFILLMENT_CONFIRMED", logs[0].Action)
	require.Equal(t, "admin:42", logs[0].Operator)
	require.Contains(t, logs[0].Detail, `"notification_email_sent":false`)

	subscriptionCount, err := client.UserSubscription.Query().Count(ctx)
	require.NoError(t, err)
	require.Zero(t, subscriptionCount)
}

func TestConfirmManualSubscriptionFulfillmentRejectsBalanceOrder(t *testing.T) {
	ctx := context.Background()
	client := newPaymentManualFulfillmentTestClient(t)
	order := createPaymentManualFulfillmentOrder(t, ctx, client, payment.OrderTypeBalance, OrderStatusPaid)
	svc := NewPaymentService(client, nil, nil, nil, nil, nil, nil, nil, nil)

	_, err := svc.ConfirmManualSubscriptionFulfillment(ctx, order.ID, "admin:42")
	require.Error(t, err)
	require.True(t, infraerrors.IsBadRequest(err))

	stored, getErr := client.PaymentOrder.Get(ctx, order.ID)
	require.NoError(t, getErr)
	require.Equal(t, OrderStatusPaid, stored.Status)
}

func TestConfirmManualSubscriptionFulfillmentRejectsNonPaidStatus(t *testing.T) {
	ctx := context.Background()
	client := newPaymentManualFulfillmentTestClient(t)
	order := createPaymentManualFulfillmentOrder(t, ctx, client, payment.OrderTypeSubscription, OrderStatusCompleted)
	svc := NewPaymentService(client, nil, nil, nil, nil, nil, nil, nil, nil)

	_, err := svc.ConfirmManualSubscriptionFulfillment(ctx, order.ID, "admin:42")
	require.Error(t, err)
	require.True(t, infraerrors.IsConflict(err))
}

func TestConfirmManualSubscriptionFulfillmentRejectsSecondConfirmation(t *testing.T) {
	ctx := context.Background()
	client := newPaymentManualFulfillmentTestClient(t)
	order := createPaymentManualFulfillmentOrder(t, ctx, client, payment.OrderTypeSubscription, OrderStatusPaid)
	svc := NewPaymentService(client, nil, nil, nil, nil, nil, nil, nil, nil)

	_, err := svc.ConfirmManualSubscriptionFulfillment(ctx, order.ID, "admin:42")
	require.NoError(t, err)

	_, err = svc.ConfirmManualSubscriptionFulfillment(ctx, order.ID, "admin:43")
	require.Error(t, err)
	require.True(t, infraerrors.IsConflict(err))

	logs, err := client.PaymentAuditLog.Query().
		Where(paymentauditlog.OrderIDEQ(strconv.FormatInt(order.ID, 10))).
		All(ctx)
	require.NoError(t, err)
	require.Len(t, logs, 1)
}

func newPaymentManualFulfillmentTestClient(t *testing.T) *dbent.Client {
	t.Helper()

	db, err := sql.Open("sqlite", "file:payment_manual_fulfillment_"+strconv.FormatInt(time.Now().UnixNano(), 10)+"?mode=memory&cache=shared&_fk=1")
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })

	_, err = db.Exec("PRAGMA foreign_keys = ON")
	require.NoError(t, err)

	drv := entsql.OpenDB(dialect.SQLite, db)
	client := enttest.NewClient(t, enttest.WithOptions(dbent.Driver(drv)))
	t.Cleanup(func() { _ = client.Close() })
	return client
}

func createPaymentManualFulfillmentOrder(
	t *testing.T,
	ctx context.Context,
	client *dbent.Client,
	orderType string,
	status string,
) *dbent.PaymentOrder {
	t.Helper()
	suffix := strconv.FormatInt(time.Now().UnixNano(), 10)
	user, err := client.User.Create().
		SetEmail("manual-fulfillment-" + suffix + "@example.com").
		SetPasswordHash("hash").
		SetUsername("manual-fulfillment-user").
		Save(ctx)
	require.NoError(t, err)

	builder := client.PaymentOrder.Create().
		SetUserID(user.ID).
		SetUserEmail(user.Email).
		SetUserName(user.Username).
		SetAmount(9.99).
		SetPayAmount(69.99).
		SetFeeRate(0).
		SetRechargeCode("PAY-MANUAL-" + suffix).
		SetOutTradeNo("sub2_manual_" + suffix).
		SetPaymentType(payment.TypeAlipay).
		SetPaymentTradeNo("trade-manual-" + suffix).
		SetOrderType(orderType).
		SetStatus(status).
		SetExpiresAt(time.Now().Add(time.Hour)).
		SetClientIP("127.0.0.1").
		SetSrcHost("console.example.com")
	if orderType == payment.OrderTypeSubscription {
		builder.SetPlanID(10).SetSubscriptionGroupID(7).SetSubscriptionDays(30)
	}
	if status == OrderStatusPaid {
		builder.SetPaidAt(time.Now().Add(-time.Minute))
	}
	if status == OrderStatusCompleted {
		builder.SetPaidAt(time.Now().Add(-time.Minute)).SetCompletedAt(time.Now())
	}
	order, err := builder.Save(ctx)
	require.NoError(t, err)
	return order
}
