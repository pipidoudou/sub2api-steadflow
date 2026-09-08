// Package distributor - user_api_key_service_test.go
//
// Round 3 修复：补充 UserAPIKeyService.IssueApiKey 的单测覆盖（QA Round 2 P1 残留）。
//
// 测试覆盖：
//  1. 「existing active & not expired 走 revoke+re-issue」分支
//     → 先签发 → 手动把 expires_at 设为过去 → 再签发 → 旧 key 变 'revoked'，
//     新 key 创建成功，且两次返回的明文不同。
//  2. 新 user 首次签发：直接创建 active key
//  3. active 且未过期的旧 key：直接 revoke + 重新签发（不依赖 expires_at 改写）
//  4. userID<=0 错误路径
//  5. IsUserAPIKeyExpired 纯函数行为（nil、空 expires_at、未过期、已过期）
//  6. NewUserAPIKeyService 加密 key 校验（长度、格式）
//
// 实现策略：
//
//	使用 in-memory SQLite + ent 自动迁移（项目内 6+ 测试在用的成熟模式）。
//	生产代码使用 *dbent.Client 具体类型，无法注入接口 mock，
//	in-memory SQLite 是项目内标准的「不需要起真实 DB」方案。
//	未引入新依赖（modernc.org/sqlite 已在 go.mod）。
package distributor

import (
	"context"
	"database/sql"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/apikey"
	"github.com/Wei-Shaw/sub2api/ent/enttest"
	"github.com/Wei-Shaw/sub2api/ent/user"
	"github.com/Wei-Shaw/sub2api/ent/userapikey"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	_ "modernc.org/sqlite"
)

// 32 字节（256 bit）的固定 AES key，hex 编码后 64 字符。
// 所有本测试文件共用同一个 key 即可（不涉及真实安全场景）。
const testAPIKeyEncryptionKeyHex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

// newUserAPIKeyTestClient 返回绑定 in-memory SQLite 的 ent.Client。
func newUserAPIKeyTestClient(t *testing.T) *dbent.Client {
	t.Helper()

	dbName := fmt.Sprintf(
		"file:%s?mode=memory&cache=shared",
		strings.NewReplacer("/", "_", " ", "_").Replace(t.Name()),
	)
	db, err := sql.Open("sqlite", dbName)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	if _, err := db.Exec("PRAGMA foreign_keys = ON"); err != nil {
		t.Fatalf("enable foreign keys: %v", err)
	}

	drv := entsql.OpenDB(dialect.SQLite, db)
	client := enttest.NewClient(t, enttest.WithOptions(dbent.Driver(drv)))
	t.Cleanup(func() { _ = client.Close() })
	return client
}

// mustNewUserAPIKeyService 构造测试用 UserAPIKeyService。
// 失败时用 t.Fatalf 直接终止当前测试（避免每个 case 重复错误处理）。
func mustNewUserAPIKeyService(t *testing.T, client *dbent.Client) *UserAPIKeyService {
	t.Helper()
	svc, err := NewUserAPIKeyService(client, nil, testAPIKeyEncryptionKeyHex)
	if err != nil {
		t.Fatalf("NewUserAPIKeyService failed: %v", err)
	}
	return svc
}

func seedGroup(t *testing.T, client *dbent.Client, name string) int64 {
	t.Helper()
	group, err := client.Group.Create().
		SetName(name).
		Save(context.Background())
	if err != nil {
		t.Fatalf("seed group %q: %v", name, err)
	}
	return int64(group.ID)
}

// seedUser 创建一个测试用 user（email/username/password_hash 最小可用）。
// 返回 ent user 的 ID（int64）。
func seedUser(t *testing.T, client *dbent.Client, email string) int64 {
	t.Helper()
	u, err := client.User.Create().
		SetEmail(email).
		SetUsername("u_" + strings.ReplaceAll(email, "@", "_at_")).
		SetPasswordHash(strings.Repeat("h", 64)).
		Save(context.Background())
	if err != nil {
		t.Fatalf("seed user %q: %v", email, err)
	}
	return int64(u.ID)
}

// TestIssueApiKey_ExpiredKeyThenReissue_CreatesNewActiveKey 覆盖 QA Round 2 P1 残留主用例：
//  1. 先签发一次 API key；
//  2. 模拟「key 已过期」现实脏数据：把旧 key 的 status 改为 'expired'
//     （生产侧通常由定时任务把 expires_at < now() 的 active key 标记为 'expired'）；
//  3. 再签发：query WHERE status='active' 找不到旧 key → 直接走新建分支；
//  4. 验证：旧 key 保持 'expired'（不再被 re-issue 路径误改），新 key 是 'active'；
//  5. 用户名下当前 active key 只有 1 条，且两次返回的明文不同。
//
// 注意：生产代码的 revoke 逻辑仅对「active 且未过期」生效；本测试覆盖的是
// 「expired-active 已被外部清理后」的正常 re-issue 路径，与
// TestIssueApiKey_ExistingActiveKeyRevokedAndReissued 互补。
func TestIssueApiKey_ExpiredKeyThenReissue_CreatesNewActiveKey(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)
	userID := seedUser(t, client, "expired-reissue@example.com")
	groupID := seedGroup(t, client, "expired-reissue")

	// 第一次签发
	first, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("first IssueApiKey failed: %v", err)
	}
	if !strings.HasPrefix(first, "sk-") {
		t.Fatalf("expected plaintext prefix 'sk-', got %q", first)
	}

	// 验证：DB 中应只有 1 条 active key
	activeKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		All(ctx)
	if err != nil {
		t.Fatalf("query active keys: %v", err)
	}
	if len(activeKeys) != 1 {
		t.Fatalf("after first issue: expected 1 active key, got %d", len(activeKeys))
	}
	oldKeyID := activeKeys[0].ID

	// 模拟外部清理任务：把旧 key 标记为 'expired'（status + expires_at 都改）。
	// 这一步代表「key 过期后被业务侧清扫」的现实场景。
	past := time.Now().Add(-1 * time.Hour)
	if _, err := client.UserAPIKey.UpdateOneID(oldKeyID).
		SetStatus("expired").
		SetExpiresAt(past).
		Save(ctx); err != nil {
		t.Fatalf("mark old key expired: %v", err)
	}

	// 第二次签发：query WHERE status='active' 找不到旧 key（已被标记 expired），
	// 不进入 revoke 分支，直接走 Create → 新 active key 创建成功。
	second, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("second IssueApiKey failed: %v", err)
	}
	if first == second {
		t.Fatalf("expected different plaintext on second issue, got identical: %q", first)
	}
	if !strings.HasPrefix(second, "sk-") {
		t.Fatalf("expected plaintext prefix 'sk-', got %q", second)
	}

	// 验证：DB 中现在有 2 条 key（旧 key status='expired'，新 key status='active'）。
	allKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID)).
		Order(userapikey.ByCreatedAt()).
		All(ctx)
	if err != nil {
		t.Fatalf("query all keys: %v", err)
	}
	if len(allKeys) != 2 {
		t.Fatalf("after second issue: expected 2 total keys, got %d", len(allKeys))
	}
	if allKeys[0].Status != "expired" {
		t.Fatalf("old key status should remain 'expired', got %q", allKeys[0].Status)
	}
	if allKeys[1].Status != "active" {
		t.Fatalf("new key status should be 'active', got %q", allKeys[1].Status)
	}

	// 验证：当前 active key 只有 1 条
	newActiveKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		All(ctx)
	if err != nil {
		t.Fatalf("query active keys after reissue: %v", err)
	}
	if len(newActiveKeys) != 1 {
		t.Fatalf("expected exactly 1 active key after reissue, got %d", len(newActiveKeys))
	}
	if newActiveKeys[0].ExpiresAt == nil || !newActiveKeys[0].ExpiresAt.After(time.Now()) {
		t.Fatalf("new active key expires_at should be in the future, got %v", newActiveKeys[0].ExpiresAt)
	}

	// 验证：返回的明文可以解密（用 svc.Decrypt）
	decrypted, err := svc.Decrypt(*newActiveKeys[0].KeyEncrypted)
	if err != nil {
		t.Fatalf("Decrypt new key: %v", err)
	}
	if decrypted != second {
		t.Fatalf("decrypted plaintext %q != returned plaintext %q", decrypted, second)
	}
}

// TestIssueApiKey_ExistingActiveKeyRevokedAndReissued 覆盖 production code 关键分支：
// existing active && not expired → 先 revoke 再创建新 key。
// 本测试不依赖 expires_at 改写，直接验证「活跃未过期」分支的 revoke+re-issue 行为。
func TestIssueApiKey_ExistingActiveKeyRevokedAndReissued(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)
	userID := seedUser(t, client, "active-revoke@example.com")
	groupID := seedGroup(t, client, "active-revoke")

	// 第一次签发
	first, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("first IssueApiKey failed: %v", err)
	}

	// 第二次签发：旧 key 是 active 且未过期 → 走 revoke + re-issue 分支
	second, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("second IssueApiKey failed: %v", err)
	}
	if first == second {
		t.Fatalf("expected different plaintext, got identical: %q", first)
	}

	// 验证：DB 状态 = 旧 key revoked + 新 key active + 当前 active 只 1 条
	// 注意：使用 userapikey.ByCreatedAt 而非 dbent.Asc —— ent 的 typed OrderOption 接口
	allKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID)).
		Order(userapikey.ByCreatedAt()).
		All(ctx)
	if err != nil {
		t.Fatalf("query all keys: %v", err)
	}
	if len(allKeys) != 2 {
		t.Fatalf("expected 2 total keys after re-issue, got %d", len(allKeys))
	}

	revokedCount := 0
	activeCount := 0
	for _, k := range allKeys {
		switch k.Status {
		case "revoked":
			revokedCount++
		case "active":
			activeCount++
		}
	}
	if revokedCount != 1 {
		t.Fatalf("expected exactly 1 revoked key, got %d", revokedCount)
	}
	if activeCount != 1 {
		t.Fatalf("expected exactly 1 active key, got %d", activeCount)
	}

	// 旧 key（created_at 较早）必须是 revoked
	if allKeys[0].Status != "revoked" {
		t.Fatalf("oldest key should be revoked, got status=%q", allKeys[0].Status)
	}
	// 新 key 必须是 active
	if allKeys[1].Status != "active" {
		t.Fatalf("newest key should be active, got status=%q", allKeys[1].Status)
	}
}

// TestIssueApiKey_NewUser_CreatesSingleActive 首次签发 happy path。
func TestIssueApiKey_NewUser_CreatesSingleActive(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)
	userID := seedUser(t, client, "fresh@example.com")
	groupID := seedGroup(t, client, "fresh")
	if _, err := client.User.UpdateOneID(userID).SetStatus("inactive").Save(ctx); err != nil {
		t.Fatalf("set user inactive: %v", err)
	}

	plaintext, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("IssueApiKey failed: %v", err)
	}
	if !strings.HasPrefix(plaintext, "sk-") {
		t.Fatalf("expected plaintext prefix 'sk-', got %q", plaintext)
	}
	if len(plaintext) != len("sk-")+64 {
		t.Fatalf("expected plaintext length %d, got %d", len("sk-")+64, len(plaintext))
	}

	keys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID)).
		All(ctx)
	if err != nil {
		t.Fatalf("query keys: %v", err)
	}
	if len(keys) != 1 {
		t.Fatalf("expected 1 key, got %d", len(keys))
	}
	if keys[0].Status != "active" {
		t.Fatalf("expected status=active, got %q", keys[0].Status)
	}
	if keys[0].ExpiresAt == nil {
		t.Fatalf("expected non-nil expires_at")
	}
	// 30 天硬过期：与生产代码一致
	expectedDelta := 30 * 24 * time.Hour
	actualDelta := time.Until(*keys[0].ExpiresAt)
	if actualDelta < expectedDelta-time.Minute || actualDelta > expectedDelta+time.Minute {
		t.Fatalf("expires_at delta from now = %v, expected ~%v", actualDelta, expectedDelta)
	}

	runtimeKeys, err := client.APIKey.Query().
		Where(apikey.UserIDEQ(userID), apikey.GroupIDEQ(groupID)).
		All(ctx)
	if err != nil {
		t.Fatalf("query runtime api keys: %v", err)
	}
	if len(runtimeKeys) != 1 {
		t.Fatalf("expected 1 runtime api key, got %d", len(runtimeKeys))
	}
	if runtimeKeys[0].Key != plaintext {
		t.Fatalf("runtime api key should equal delivered plaintext")
	}
	if runtimeKeys[0].Status != "active" {
		t.Fatalf("expected runtime api key active, got %q", runtimeKeys[0].Status)
	}
	userAfter, err := client.User.Get(ctx, userID)
	if err != nil {
		t.Fatalf("get user after issue: %v", err)
	}
	if userAfter.Status != "active" {
		t.Fatalf("expected user activated, got %q", userAfter.Status)
	}
}

// TestIssueApiKey_InvalidUserID_ReturnsError 错误路径：userID <=0 → 直接报错。
func TestIssueApiKey_InvalidUserID_ReturnsError(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)

	for _, uid := range []int64{0, -1, -100} {
		plaintext, err := svc.IssueApiKey(ctx, uid, 1)
		if err == nil {
			t.Fatalf("userID=%d: expected error, got plaintext=%q", uid, plaintext)
		}
		if plaintext != "" {
			t.Fatalf("userID=%d: expected empty plaintext on error, got %q", uid, plaintext)
		}
	}
}

// TestIssueApiKey_ActiveKeyExpired_RevokesAndReissues 覆盖 Round 4 P1 时序修复：
//
// 真实脏数据场景：旧 key 已被时间推到 expires_at < now()，但 status 仍是
// 'active'（项目内无 cron 自动把过期 active 标记为 expired——这是 QA 复验确认的
// 事实）。Round 3 修复前，IssueApiKey 在此场景下：
//  1. query WHERE status='active' 命中旧 key
//  2. IsUserAPIKeyExpired(existing)=true → revoke 分支跳过
//  3. INSERT 新 active key → partial unique index 冲突 → distributor_order_service
//     自动签发链路 30+ 天后批量失败
//
// Round 4 修复后：去掉 `&& !IsUserAPIKeyExpired(existing)` 条件，任何 active
// existing key 都先 revoke 再签发，避免 partial unique index 冲突。
//
// 本测试严格模拟真实时序（不手动改 status），直接 UPDATE expires_at 到过去。
func TestIssueApiKey_ActiveKeyExpired_RevokesAndReissues(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)
	userID := seedUser(t, client, "active-expired-revoke@example.com")
	groupID := seedGroup(t, client, "active-expired-revoke")

	// 第一次签发：模拟 T0 用户签发 → status='active', expires_at=T0+30d
	first, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("first IssueApiKey failed: %v", err)
	}
	if !strings.HasPrefix(first, "sk-") {
		t.Fatalf("expected plaintext prefix 'sk-', got %q", first)
	}

	// 验证第一次签发后：DB 中只有 1 条 active key
	activeKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		All(ctx)
	if err != nil {
		t.Fatalf("query active keys after first issue: %v", err)
	}
	if len(activeKeys) != 1 {
		t.Fatalf("after first issue: expected 1 active key, got %d", len(activeKeys))
	}
	oldKeyID := activeKeys[0].ID

	// 真实时序：模拟时间流逝到 T0+30d 后，**只**把 expires_at 改为过去，
	// 不动 status。这是 Round 4 P1 bug 的核心场景——项目内无 cron 清理。
	past := time.Now().Add(-24 * time.Hour)
	if _, err := client.UserAPIKey.UpdateOneID(oldKeyID).
		SetExpiresAt(past).
		Save(ctx); err != nil {
		t.Fatalf("backdate expires_at: %v", err)
	}

	// 验证：旧 key 仍是 status='active'（这是修复前的 bug 触发条件）
	oldKeyCheck, err := client.UserAPIKey.Get(ctx, oldKeyID)
	if err != nil {
		t.Fatalf("get old key after backdate: %v", err)
	}
	if oldKeyCheck.Status != "active" {
		t.Fatalf("old key status should still be 'active' (no cron), got %q", oldKeyCheck.Status)
	}
	if oldKeyCheck.ExpiresAt == nil || !oldKeyCheck.ExpiresAt.Before(time.Now()) {
		t.Fatalf("old key expires_at should be in the past, got %v", oldKeyCheck.ExpiresAt)
	}
	if !IsUserAPIKeyExpired(oldKeyCheck) {
		t.Fatal("IsUserAPIKeyExpired should be true after backdate")
	}

	// 第二次签发：Round 4 修复后，任何 active existing key 都应被 revoke，
	// 然后新 active key 创建成功。修复前此调用会因 partial unique index 冲突失败。
	second, err := svc.IssueApiKey(ctx, userID, groupID)
	if err != nil {
		t.Fatalf("second IssueApiKey failed (Round 4 fix should handle expired-active): %v", err)
	}
	if first == second {
		t.Fatalf("expected different plaintext on re-issue, got identical: %q", first)
	}
	if !strings.HasPrefix(second, "sk-") {
		t.Fatalf("expected plaintext prefix 'sk-', got %q", second)
	}

	// 验证：旧 key 状态变 'revoked'
	oldKeyAfter, err := client.UserAPIKey.Get(ctx, oldKeyID)
	if err != nil {
		t.Fatalf("get old key after re-issue: %v", err)
	}
	if oldKeyAfter.Status != "revoked" {
		t.Fatalf("old key should be revoked after re-issue, got status=%q", oldKeyAfter.Status)
	}

	// 验证：用户当前 active key 只有 1 条（partial unique index 要求 active 唯一）
	newActiveKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		All(ctx)
	if err != nil {
		t.Fatalf("query active keys after re-issue: %v", err)
	}
	if len(newActiveKeys) != 1 {
		t.Fatalf("expected exactly 1 active key after re-issue, got %d", len(newActiveKeys))
	}
	if newActiveKeys[0].ID == oldKeyID {
		t.Fatalf("new active key should be a different record from old key")
	}

	// 验证：新 active key 的 expires_at 在未来（30 天硬过期）
	newKey := newActiveKeys[0]
	if newKey.ExpiresAt == nil || !newKey.ExpiresAt.After(time.Now()) {
		t.Fatalf("new key expires_at should be in the future, got %v", newKey.ExpiresAt)
	}

	// 验证：返回的明文可以解密（与新 key 的加密字段匹配）
	decrypted, err := svc.Decrypt(*newKey.KeyEncrypted)
	if err != nil {
		t.Fatalf("Decrypt new key: %v", err)
	}
	if decrypted != second {
		t.Fatalf("decrypted plaintext %q != returned plaintext %q", decrypted, second)
	}

	// 验证：DB 中共 2 条 key（旧 revoked + 新 active）
	allKeys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID)).
		Order(userapikey.ByCreatedAt()).
		All(ctx)
	if err != nil {
		t.Fatalf("query all keys: %v", err)
	}
	if len(allKeys) != 2 {
		t.Fatalf("expected 2 total keys, got %d", len(allKeys))
	}
	if allKeys[0].Status != "revoked" {
		t.Fatalf("oldest key should be revoked, got %q", allKeys[0].Status)
	}
	if allKeys[1].Status != "active" {
		t.Fatalf("newest key should be active, got %q", allKeys[1].Status)
	}
}

// TestNewUserAPIKeyService_RejectsInvalidEncryptionKey 构造器校验：key 长度错误。
func TestNewUserAPIKeyService_RejectsInvalidEncryptionKey(t *testing.T) {
	client := newUserAPIKeyTestClient(t)

	// 16 字节 hex（应该失败：必须 32 字节）
	shortKey := hex.EncodeToString(make([]byte, 16))
	if _, err := NewUserAPIKeyService(client, nil, shortKey); err == nil {
		t.Fatal("expected error for 16-byte key, got nil")
	}

	// 64 字节 hex（应该失败：必须 32 字节）
	longKey := hex.EncodeToString(make([]byte, 64))
	if _, err := NewUserAPIKeyService(client, nil, longKey); err == nil {
		t.Fatal("expected error for 64-byte key, got nil")
	}

	// 非 hex 字符串（应该失败：解码错误）
	if _, err := NewUserAPIKeyService(client, nil, "not-hex-string!@#$"); err == nil {
		t.Fatal("expected error for non-hex key, got nil")
	}

	// 正确 32 字节 hex → 成功
	goodKey := hex.EncodeToString(make([]byte, 32))
	svc, err := NewUserAPIKeyService(client, nil, goodKey)
	if err != nil {
		t.Fatalf("32-byte key should succeed, got: %v", err)
	}
	if svc == nil {
		t.Fatal("expected non-nil service")
	}
}

// TestIsUserAPIKeyExpired_AllBranches 纯函数覆盖。
func TestIsUserAPIKeyExpired_AllBranches(t *testing.T) {
	// nil 输入 → 不过期（防御性兜底）
	if IsUserAPIKeyExpired(nil) {
		t.Fatal("nil key should not be considered expired")
	}

	// expires_at == nil → 不过期
	k := &dbent.UserAPIKey{}
	if IsUserAPIKeyExpired(k) {
		t.Fatal("key with nil ExpiresAt should not be considered expired")
	}

	// expires_at 在未来 → 不过期
	future := time.Now().Add(1 * time.Hour)
	kFuture := &dbent.UserAPIKey{ExpiresAt: &future}
	if IsUserAPIKeyExpired(kFuture) {
		t.Fatal("key with future ExpiresAt should not be considered expired")
	}

	// expires_at 在过去 → 过期
	past := time.Now().Add(-1 * time.Hour)
	kPast := &dbent.UserAPIKey{ExpiresAt: &past}
	if !IsUserAPIKeyExpired(kPast) {
		t.Fatal("key with past ExpiresAt should be considered expired")
	}
}

// TestUserAPIKeyService_Revoke_ChangesStatus 配套覆盖 Revoke 方法。
func TestUserAPIKeyService_Revoke_ChangesStatus(t *testing.T) {
	ctx := context.Background()
	client := newUserAPIKeyTestClient(t)
	svc := mustNewUserAPIKeyService(t, client)
	userID := seedUser(t, client, "revoke@example.com")
	groupID := seedGroup(t, client, "revoke")

	// 签发
	if _, err := svc.IssueApiKey(ctx, userID, groupID); err != nil {
		t.Fatalf("IssueApiKey: %v", err)
	}

	keys, err := client.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		All(ctx)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if len(keys) != 1 {
		t.Fatalf("expected 1 active key, got %d", len(keys))
	}
	keyID := int64(keys[0].ID)

	// 吊销
	if err := svc.Revoke(ctx, keyID); err != nil {
		t.Fatalf("Revoke: %v", err)
	}

	// 验证
	got, err := client.UserAPIKey.Get(ctx, keyID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status != "revoked" {
		t.Fatalf("expected status=revoked, got %q", got.Status)
	}
}

// 编译期引用校验：防止 import 被"瘦身优化"误删。
var (
	_ = user.EmailEQ
	_ = userapikey.UserIDEQ
	_ = userapikey.StatusEQ
	_ = userapikey.ByCreatedAt
)
