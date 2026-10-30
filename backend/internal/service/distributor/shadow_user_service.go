// Package distributor 提供分销商相关的服务（v2.2 §4.2）。
//
// ShadowUserService.EnsureOrderUser - 兜底创建 shadow user。
// 关键点：
//   - 幂等：webhook 重放不重复创建
//   - 下单阶段先创建为 inactive，避免未履约用户登录
//   - 自动发货成功后由 UserAPIKeyService 激活，使用交付邮件中的独立初始密码登录
package distributor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"

	"golang.org/x/crypto/bcrypt"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/user"
)

// ShadowUserService 负责分销商订单的 shadow user 兜底创建。
type ShadowUserService struct {
	db     *dbent.Client
	logger *slog.Logger
}

// NewShadowUserService 构造 ShadowUserService。
func NewShadowUserService(db *dbent.Client, logger *slog.Logger) *ShadowUserService {
	if logger == nil {
		logger = slog.Default()
	}
	return &ShadowUserService{db: db, logger: logger}
}

type initialLoginOptions struct {
	OrderRef string
	Encrypt  func(string) (string, error)
}

type initialLoginCredential struct {
	OrderRef string `json:"orderRef"`
	Password string `json:"password"`
}

// EnsureOrderUser 基于 email 兜底创建 shadow user（幂等）。
//
// 流程（v2.2 §4.3.3）：
//  1. SELECT users WHERE email=? AND deleted_at IS NULL
//  2. 命中 → 返回 userID（幂等）
//  3. 未命中 → INSERT (email, password_hash=bcrypt(random_password), status='inactive')
//  4. 写 audit_log: shadow_user_created_by=distributor
//  5. 返回 userID
//
// 返回：userID（int64），isNewlyCreated（bool）
func (s *ShadowUserService) EnsureOrderUser(ctx context.Context, email string, options ...initialLoginOptions) (int64, bool, error) {
	if email == "" {
		return 0, false, errors.New("email required")
	}

	// 1. 查重。
	// 注意：users 表已挂 SoftDeleteMixin 拦截器，自动过滤 deleted_at IS NULL，
	// 因此这里只追加 user.DeletedAtIsNil() 是冗余强化，避免 soft-delete 拦截器被未来改写。
	// 注意：User 字段是 password_hash（非 password），对应 ent 生成方法 SetPasswordHash。
	existing, err := s.db.User.Query().
		Where(user.EmailEQ(email), user.DeletedAtIsNil()).
		Only(ctx)
	if err == nil && existing != nil {
		s.logger.Info("shadow user: existing user found, reuse",
			"user_id", existing.ID,
			"email", email,
		)
		return int64(existing.ID), false, nil
	}
	if err != nil && !dbent.IsNotFound(err) {
		return 0, false, fmt.Errorf("query existing user: %w", err)
	}

	// New accounts receive a cryptographically random password; existing accounts are never changed.
	plaintext, err := generateRandomPassword(16)
	if err != nil {
		return 0, false, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(plaintext), bcrypt.DefaultCost)
	if err != nil {
		return 0, false, fmt.Errorf("hash initial password: %w", err)
	}
	encrypted := ""
	if len(options) > 0 {
		data, err := json.Marshal(initialLoginCredential{OrderRef: options[0].OrderRef, Password: plaintext})
		if err != nil {
			return 0, false, err
		}
		encrypted, err = options[0].Encrypt(string(data))
		if err != nil {
			return 0, false, fmt.Errorf("encrypt initial password: %w", err)
		}
	}
	username := "shadow_" + hex.EncodeToString([]byte(email)[:minInt(8, len(email))])
	// 上 username 长度限制（DB schema 通常 64 字符）
	if len(username) > 60 {
		username = username[:60]
	}

	created, err := s.db.User.Create().
		SetUsername(username).
		SetEmail(email).
		SetPasswordHash(string(passwordHash)).
		SetDistributorLoginEncrypted(encrypted).
		SetStatus("inactive"). // 下单兜底阶段保持 inactive，发货成功后激活
		SetNotes("created_by=distributor (site BFF)").
		Save(ctx)
	if err != nil {
		// A concurrent purchase may have created the same email first. Reuse it without changing its password.
		if existing, lookupErr := s.db.User.Query().Where(user.EmailEQ(email), user.DeletedAtIsNil()).Only(ctx); lookupErr == nil {
			return existing.ID, false, nil
		}
		return 0, false, fmt.Errorf("create shadow user: %w", err)
	}

	// 3. audit_log（这里简化：直接通过日志记录；真实实现可以写入 audit_log 表）
	s.logger.Info("shadow user: created",
		"user_id", created.ID,
		"email", email,
		"actor", "system:distributor",
	)

	return int64(created.ID), true, nil
}

// Entropy failure must fail closed, never fall back to a predictable password.
func generateRandomPassword(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate initial password: %w", err)
	}
	return hex.EncodeToString(b), nil
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
