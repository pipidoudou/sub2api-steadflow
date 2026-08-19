// Package distributor - shadow_user_service_test.go
//
// Round 3 修复：补充 ShadowUserService.EnsureOrderUser 的单测覆盖（QA Round 2 P1 残留）。
//
// 测试覆盖：
//  1. 新建 shadow user：传 email A → 返回 (id1, true, nil)，DB 中多 1 条 user
//  2. 幂等：再传 email A → 返回 (id1, false, nil)，DB 中仍只 1 条 user
//  3. 空 email 错误路径
//
// 实现策略：
//
//	使用 in-memory SQLite + ent 自动迁移（项目内 6+ 测试在用的成熟模式，
//	见 internal/service/payment_config_service_test.go 等）。
//	生产代码使用 *dbent.Client 具体类型，无法注入接口 mock，
//	in-memory SQLite 是项目内标准的「不需要起真实 DB」方案。
//	未引入新依赖（modernc.org/sqlite 已在 go.mod）。
package distributor

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"testing"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/enttest"
	"github.com/Wei-Shaw/sub2api/ent/user"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	_ "modernc.org/sqlite"
)

// newShadowUserTestClient 返回一个绑定 in-memory SQLite 的 ent.Client。
// 每个测试函数拿到独立的内存 DB（file: <test_name>?mode=memory&cache=shared）。
func newShadowUserTestClient(t *testing.T) *dbent.Client {
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

// TestEnsureOrderUser_NewEmail_CreatesShadowUser 覆盖 Case 1：
// 首次传 email → 触发创建分支，返回 (id, true, nil)。
func TestEnsureOrderUser_NewEmail_CreatesShadowUser(t *testing.T) {
	ctx := context.Background()
	client := newShadowUserTestClient(t)
	svc := NewShadowUserService(client, nil)

	email := "buyer-A@example.com"

	id, created, err := svc.EnsureOrderUser(ctx, email)
	if err != nil {
		t.Fatalf("EnsureOrderUser returned unexpected error: %v", err)
	}
	if !created {
		t.Fatalf("expected created=true on first call, got false (id=%d)", id)
	}
	if id <= 0 {
		t.Fatalf("expected positive user ID, got %d", id)
	}

	// 验证 DB 状态：1 条 user，email/status/notes 正确。
	all, err := client.User.Query().Where(user.EmailEQ(email)).All(ctx)
	if err != nil {
		t.Fatalf("query user: %v", err)
	}
	if len(all) != 1 {
		t.Fatalf("expected 1 user row, got %d", len(all))
	}
	got := all[0]
	if int64(got.ID) != id {
		t.Fatalf("returned id=%d but DB row has id=%d", id, got.ID)
	}
	if got.Status != "inactive" {
		t.Fatalf("expected status=inactive, got %q", got.Status)
	}
	if got.Email != email {
		t.Fatalf("expected email=%q, got %q", email, got.Email)
	}
	// shadow user 必须有 password_hash（随机 32 字节 sha256 → 64 hex）
	if len(got.PasswordHash) != 64 {
		t.Fatalf("expected password_hash length=64 (sha256 hex), got %d", len(got.PasswordHash))
	}
	// username 必须是 "shadow_" 前缀
	if !strings.HasPrefix(got.Username, "shadow_") {
		t.Fatalf("expected username prefix 'shadow_', got %q", got.Username)
	}
	// notes 必须包含 created_by=distributor（审计追溯）
	if !strings.Contains(got.Notes, "created_by=distributor") {
		t.Fatalf("expected notes to contain 'created_by=distributor', got %q", got.Notes)
	}
}

// TestEnsureOrderUser_ExistingEmail_Idempotent 覆盖 Case 2：
// 第二次传相同 email → 命中查询分支，返回相同 id 且 created=false，
// DB 中 user 数量仍为 1（无重复行）。
func TestEnsureOrderUser_ExistingEmail_Idempotent(t *testing.T) {
	ctx := context.Background()
	client := newShadowUserTestClient(t)
	svc := NewShadowUserService(client, nil)

	email := "buyer-B@example.com"

	// 第一次调用：创建。
	id1, created1, err := svc.EnsureOrderUser(ctx, email)
	if err != nil {
		t.Fatalf("first EnsureOrderUser failed: %v", err)
	}
	if !created1 || id1 <= 0 {
		t.Fatalf("first call expected (id>0, true), got (id=%d, created=%v)", id1, created1)
	}

	// 第二次调用：幂等。
	id2, created2, err := svc.EnsureOrderUser(ctx, email)
	if err != nil {
		t.Fatalf("second EnsureOrderUser failed: %v", err)
	}
	if created2 {
		t.Fatalf("second call expected created=false (idempotent), got true")
	}
	if id1 != id2 {
		t.Fatalf("idempotency violated: first id=%d, second id=%d", id1, id2)
	}

	// 验证 DB：仍只有 1 条 user。
	count, err := client.User.Query().Where(user.EmailEQ(email)).Count(ctx)
	if err != nil {
		t.Fatalf("count user: %v", err)
	}
	if count != 1 {
		t.Fatalf("expected exactly 1 user row after idempotent call, got %d", count)
	}

	// 第三次调用：继续幂等。
	id3, created3, err := svc.EnsureOrderUser(ctx, email)
	if err != nil {
		t.Fatalf("third EnsureOrderUser failed: %v", err)
	}
	if created3 || id3 != id1 {
		t.Fatalf("third call expected (id=%d, created=false), got (id=%d, created=%v)", id1, id3, created3)
	}

	// 反复调用后 DB 仍 1 行。
	count2, err := client.User.Query().Where(user.EmailEQ(email)).Count(ctx)
	if err != nil {
		t.Fatalf("count user after 3 calls: %v", err)
	}
	if count2 != 1 {
		t.Fatalf("expected 1 user row after 3 calls, got %d", count2)
	}
}

// TestEnsureOrderUser_EmptyEmail_ReturnsError 错误路径：email 空字符串 → 直接报错，
// 不会触发 DB 调用。
func TestEnsureOrderUser_EmptyEmail_ReturnsError(t *testing.T) {
	ctx := context.Background()
	client := newShadowUserTestClient(t)
	svc := NewShadowUserService(client, nil)

	id, created, err := svc.EnsureOrderUser(ctx, "")
	if err == nil {
		t.Fatalf("expected error for empty email, got nil (id=%d, created=%v)", id, created)
	}
	if id != 0 || created {
		t.Fatalf("expected zero values on error, got (id=%d, created=%v)", id, created)
	}

	// DB 中不应有任何 user 记录。
	count, err := client.User.Query().Count(ctx)
	if err != nil {
		t.Fatalf("count user: %v", err)
	}
	if count != 0 {
		t.Fatalf("expected 0 user rows after empty-email call, got %d", count)
	}
}

// TestEnsureOrderUser_DifferentEmails_ProduceDifferentUsers 边界：
// 不同 email → 创建不同的 user（确保按 email 区分，不复用）。
func TestEnsureOrderUser_DifferentEmails_ProduceDifferentUsers(t *testing.T) {
	ctx := context.Background()
	client := newShadowUserTestClient(t)
	svc := NewShadowUserService(client, nil)

	emailA := "alice@example.com"
	emailB := "bob@example.com"

	idA, createdA, err := svc.EnsureOrderUser(ctx, emailA)
	if err != nil || !createdA {
		t.Fatalf("first EnsureOrderUser(emailA) failed: id=%d created=%v err=%v", idA, createdA, err)
	}

	idB, createdB, err := svc.EnsureOrderUser(ctx, emailB)
	if err != nil || !createdB {
		t.Fatalf("first EnsureOrderUser(emailB) failed: id=%d created=%v err=%v", idB, createdB, err)
	}

	if idA == idB {
		t.Fatalf("expected different IDs for different emails, both got %d", idA)
	}

	// 两个 user 都在 DB。
	total, err := client.User.Query().Count(ctx)
	if err != nil {
		t.Fatalf("count user: %v", err)
	}
	if total != 2 {
		t.Fatalf("expected 2 user rows, got %d", total)
	}
}

// TestEnsureOrderUser_NotFoundThenRecreate_OnDeletedUser 验证 soft-delete 行为：
// 假设某历史流程软删除了同名 email 的 user，本函数应能"复活"——
// 即按 email 查不到（非 NOT_FOUND 之外的错误），会走到创建分支。
// 本测试只验证 NOT_FOUND 路径（标准幂等路径），不动软删除逻辑。
func TestEnsureOrderUser_NotFoundOnMissingEmail_TriggersCreate(t *testing.T) {
	ctx := context.Background()
	client := newShadowUserTestClient(t)
	svc := NewShadowUserService(client, nil)

	// 不预先插入任何 user，直接调用 → 走创建分支。
	id, created, err := svc.EnsureOrderUser(ctx, "ghost@example.com")
	if err != nil {
		t.Fatalf("EnsureOrderUser returned error: %v", err)
	}
	if !created {
		t.Fatalf("expected created=true, got false (id=%d)", id)
	}
	if id <= 0 {
		t.Fatalf("expected positive id, got %d", id)
	}
}

// 编译期校验：包内引用，防止 import 漂移。
var _ = dbent.IsNotFound
