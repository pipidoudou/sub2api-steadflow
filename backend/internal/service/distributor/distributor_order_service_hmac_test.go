// Package distributor - distributor_order_service_hmac_test.go
//
// Round 2 修复的 P0-2 单元测试：验证 console → BFF webhook 的 HMAC-SHA256 签名。
//
// 任务文档要求：HMAC 实现要可测试——写单测验证。
//
// 测试覆盖：
//  1. 同输入得到稳定的 sha256=hex 摘要
//  2. 不同 secret（或 body）得到不同摘要
//  3. 签名格式与 BFF verifyHmac 兼容（"sha256=" 前缀 + lowercase hex）
//  4. 模拟 console 真实用例（payload=distributor.order.fulfilled → BFF 应验签通过）
//  5. 错误 secret → BFF 端返回 401（与真实 BFF verifyHmac 行为对齐）
package distributor

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSignWebhookPayload_Deterministic(t *testing.T) {
	secret := []byte("test-secret")
	body := []byte(`{"event":"distributor.order.fulfilled","order_id":42}`)

	sig1 := SignWebhookPayload(secret, body)
	sig2 := SignWebhookPayload(secret, body)
	if sig1 != sig2 {
		t.Fatalf("expected deterministic signature, got %q vs %q", sig1, sig2)
	}
	if !strings.HasPrefix(sig1, "sha256=") {
		t.Fatalf("missing sha256= prefix: %q", sig1)
	}
}

func TestResolveOrderPlan_PrefersExplicitConsoleMapping(t *testing.T) {
	svc := &DistributorOrderService{}
	svc.SetResolvePlan(func(ctx context.Context, tier string) (int64, int64, int, error) {
		t.Fatalf("fallback resolver should not be called for explicit console mapping")
		return 0, 0, 0, nil
	})

	planID, groupID, days, err := svc.resolveOrderPlan(context.Background(), CreateDistributorOrderRequest{
		Tier:         "site_custom_codex_30d",
		GroupID:      501,
		PlanID:       601,
		ValidityDays: 30,
	})
	if err != nil {
		t.Fatalf("resolveOrderPlan returned error: %v", err)
	}
	if planID != 601 || groupID != 501 || days != 30 {
		t.Fatalf("unexpected mapping: planID=%d groupID=%d days=%d", planID, groupID, days)
	}
}

func TestResolveOrderPlan_UsesGroupIDAsPlanFallback(t *testing.T) {
	svc := &DistributorOrderService{}

	planID, groupID, days, err := svc.resolveOrderPlan(context.Background(), CreateDistributorOrderRequest{
		Tier:         "site_custom_codex_30d",
		GroupID:      502,
		ValidityDays: 60,
	})
	if err != nil {
		t.Fatalf("resolveOrderPlan returned error: %v", err)
	}
	if planID != 502 || groupID != 502 || days != 60 {
		t.Fatalf("unexpected fallback mapping: planID=%d groupID=%d days=%d", planID, groupID, days)
	}
}

func TestSignWebhookPayload_Format(t *testing.T) {
	sig := SignWebhookPayload([]byte("k"), []byte("body"))
	if !strings.HasPrefix(sig, "sha256=") {
		t.Fatalf("missing sha256= prefix: %q", sig)
	}
	hexPart := strings.TrimPrefix(sig, "sha256=")
	// 必须是 64 hex chars（SHA-256 = 32 bytes = 64 hex）
	if len(hexPart) != 64 {
		t.Fatalf("expected 64-hex signature, got %d chars: %q", len(hexPart), hexPart)
	}
	// 用 hex 解码应该能成功
	decoded, err := hex.DecodeString(hexPart)
	if err != nil {
		t.Fatalf("hex decode failed: %v", err)
	}
	if len(decoded) != 32 {
		t.Fatalf("expected 32-byte digest, got %d", len(decoded))
	}
}

func TestSignWebhookPayload_MatchesReferenceHMAC(t *testing.T) {
	// 对照手算的 HMAC-SHA256，验证实现与 crypto/hmac 标准库一致
	secret := []byte("supersecret")
	body := []byte(`{"a":1}`)
	wantMac := hmac.New(sha256.New, secret)
	wantMac.Write(body)
	wantHex := "sha256=" + hex.EncodeToString(wantMac.Sum(nil))

	got := SignWebhookPayload(secret, body)
	if got != wantHex {
		t.Fatalf("signature mismatch\n got: %s\nwant: %s", got, wantHex)
	}
}

func TestSignWebhookPayload_DifferentInputsDiffer(t *testing.T) {
	secret := []byte("k")
	bodyA := []byte(`{"a":1}`)
	bodyB := []byte(`{"a":2}`)
	secretA := []byte("k1")
	secretB := []byte("k2")

	if SignWebhookPayload(secret, bodyA) == SignWebhookPayload(secret, bodyB) {
		t.Fatal("same secret with different body should produce different sigs")
	}
	if SignWebhookPayload(secretA, bodyA) == SignWebhookPayload(secretB, bodyA) {
		t.Fatal("different secret with same body should produce different sigs")
	}
}

// TestSignWebhookPayload_EmptyInputsEdgeCases 边界：空 body / 空 secret。
func TestSignWebhookPayload_EmptyInputsEdgeCases(t *testing.T) {
	if got := SignWebhookPayload([]byte("k"), nil); !strings.HasPrefix(got, "sha256=") {
		t.Fatalf("nil body should still sign: %q", got)
	}
	if got := SignWebhookPayload(nil, []byte("body")); !strings.HasPrefix(got, "sha256=") {
		t.Fatalf("nil secret should still produce a (degenerate) signature: %q", got)
	}
	// nil secret + nil body should NOT panic
	_ = SignWebhookPayload(nil, nil)
}

// TestNotifyBFFWebhook_SignatureHeaderSet 端到端验证：模拟 console 发 webhook，
// 服务器侧验证收到的 X-Webhook-Signature 是有效 HMAC（复刻 BFF verifyHmac 逻辑）。
func TestNotifyBFFWebhook_SignatureHeaderSet(t *testing.T) {
	var (
		gotBody string
		gotSig  string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf, _ := io.ReadAll(r.Body)
		gotBody = string(buf)
		gotSig = r.Header.Get("X-Webhook-Signature")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	svc := &DistributorOrderService{
		httpClient: srv.Client(),
	}
	err := svc.notifyBFFWebhook(context.Background(), srv.URL, "shared-secret-xyz", 123, 456, "sk-test", "2026-08-01T00:00:00Z")
	if err != nil {
		t.Fatalf("notifyBFFWebhook failed: %v", err)
	}
	if gotBody == "" {
		t.Fatal("server did not receive body")
	}
	if gotSig == "" {
		t.Fatal("X-Webhook-Signature header was empty")
	}
	if !strings.HasPrefix(gotSig, "sha256=") {
		t.Fatalf("bad signature prefix: %q", gotSig)
	}
	// 服务端（模拟 BFF）独立算一次 HMAC，必须匹配
	want := SignWebhookPayload([]byte("shared-secret-xyz"), []byte(gotBody))
	if gotSig != want {
		t.Fatalf("server-side HMAC mismatch\n got: %s\nwant: %s", gotSig, want)
	}
}

func TestNotifyBFFWebhook_SendsFulfillmentPayloadForBFF(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(buf, &got); err != nil {
			t.Fatalf("invalid webhook json: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	svc := &DistributorOrderService{
		httpClient: srv.Client(),
	}
	if err := svc.notifyBFFWebhook(context.Background(), srv.URL, "shared-secret-xyz", 123, 456, "sk-real-abc", "2026-08-01T00:00:00Z"); err != nil {
		t.Fatalf("notifyBFFWebhook failed: %v", err)
	}
	if got["supplierOrderId"] != "CONSOX-123" {
		t.Fatalf("supplierOrderId mismatch: %#v", got)
	}
	if got["status"] != "fulfilled" {
		t.Fatalf("status mismatch: %#v", got)
	}
	if got["shadowUserId"].(float64) != 456 {
		t.Fatalf("shadowUserId mismatch: %#v", got)
	}
	if got["apiKey"] != "sk-real-abc" {
		t.Fatalf("apiKey missing: %#v", got)
	}
	if got["expiresAt"] != "2026-08-01T00:00:00Z" {
		t.Fatalf("expiresAt mismatch: %#v", got)
	}
}

// TestNotifyBFFWebhook_WrongSecretFails 模拟「错误 secret」场景，确认验签会失败（401）。
// 这正是 BFF 端 verifyHmac 在签名错误时返回 401 的复刻。
func TestNotifyBFFWebhook_WrongSecretFails(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf, _ := io.ReadAll(r.Body)
		body := string(buf)
		sig := r.Header.Get("X-Webhook-Signature")
		// 模拟 BFF verifyHmac：只有「正确 secret 签出的 sig」才返回 200
		want := SignWebhookPayload([]byte("correct-secret"), []byte(body))
		if hmac.Equal([]byte(sig), []byte(want)) {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusUnauthorized)
		}
	}))
	defer srv.Close()

	svc := &DistributorOrderService{
		httpClient: srv.Client(),
	}
	err := svc.notifyBFFWebhook(context.Background(), srv.URL, "wrong-secret", 1, 1, "sk-test", "2026-08-01T00:00:00Z")
	if err == nil {
		t.Fatal("expected error when webhook returns 401")
	}
	if !strings.Contains(err.Error(), "status=401") {
		t.Fatalf("expected 401 in error, got: %v", err)
	}
}

// TestNotifyBFFWebhook_NoSecretUnsignedMode dev 模式：不传 secret → header 标 "unsigned"。
func TestNotifyBFFWebhook_NoSecretUnsignedMode(t *testing.T) {
	var gotSig string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotSig = r.Header.Get("X-Webhook-Signature")
		w.WriteHeader(http.StatusOK)
		_, _ = io.Copy(io.Discard, r.Body)
	}))
	defer srv.Close()

	svc := &DistributorOrderService{
		httpClient: srv.Client(),
	}
	if err := svc.notifyBFFWebhook(context.Background(), srv.URL, "", 1, 1, "sk-test", "2026-08-01T00:00:00Z"); err != nil {
		t.Fatalf("expected 200 from dev-mode server, got: %v", err)
	}
	if gotSig != "unsigned" {
		t.Fatalf("expected header 'unsigned' in dev mode, got %q", gotSig)
	}
}

// TestNotifyBFFWebhook_HTTPStatusCheck 5xx 也应被识别为失败。
func TestNotifyBFFWebhook_HTTPStatusCheck(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	svc := &DistributorOrderService{
		httpClient: srv.Client(),
	}
	err := svc.notifyBFFWebhook(context.Background(), srv.URL, "k", 1, 1, "sk-test", "2026-08-01T00:00:00Z")
	if err == nil {
		t.Fatal("expected error when webhook returns 500")
	}
}

// sanity: 测试包内的辅助函数，签名包通过 import paths（防止 build 引用错）
var _ = bytes.NewReader
