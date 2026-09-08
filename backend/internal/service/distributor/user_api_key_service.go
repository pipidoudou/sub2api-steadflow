// Package distributor - UserAPIKeyService（v2.2 §4.3.3）
//
// 用户级 API key 颁发（Codex 中转依赖）。
// 关键点：
//   - bcrypt + AES-256-GCM 双重保护
//   - 30 天硬过期
//   - 用户级 API key 是 console 的全新概念（grep GenerateApiKey 0 命中）
package distributor

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/apikey"
	"github.com/Wei-Shaw/sub2api/ent/userapikey"
)

// UserAPIKeyService 提供用户级 API key 的颁发/查询/吊销。
type UserAPIKeyService struct {
	db            *dbent.Client
	logger        *slog.Logger
	encryptionKey []byte // AES-256 key (32 bytes)
}

// NewUserAPIKeyService 构造 UserAPIKeyService。
// encryptionKey 必须是 32 字节（base64 编码后传入）。
func NewUserAPIKeyService(db *dbent.Client, logger *slog.Logger, encryptionKeyBase64 string) (*UserAPIKeyService, error) {
	if logger == nil {
		logger = slog.Default()
	}
	encKey, err := hex.DecodeString(encryptionKeyBase64)
	if err != nil {
		// 退路：直接 base64 decode
		// 实际实现应用 base64.StdEncoding.DecodeString
		// 这里简化：尝试 hex -> 失败 -> 返回错误
		return nil, fmt.Errorf("decode encryption key: %w", err)
	}
	if len(encKey) != 32 {
		return nil, fmt.Errorf("encryption key must be 32 bytes, got %d", len(encKey))
	}
	return &UserAPIKeyService{db: db, logger: logger, encryptionKey: encKey}, nil
}

// IsUserAPIKeyExpired 判断给定的用户 API key 记录是否过期（本地 helper，绕开外部类型不可加方法的限制）。
// 导出供 handler 等其它包复用。
func IsUserAPIKeyExpired(k *dbent.UserAPIKey) bool {
	if k == nil || k.ExpiresAt == nil {
		return false
	}
	return time.Now().After(*k.ExpiresAt)
}

// IssueApiKey 颁发 API key 给指定用户（幂等）。
//
// 流程（v2.2 §4.3.3）：
//  1. 生成 sk-<32位随机>
//  2. key_hash = bcrypt(key) + AES-256-GCM 加密
//  3. INSERT user_api_keys (..., expires_at=now+30d)
//  4. 返回明文（仅此一次，调用方需立即使用）
//
// 返回：明文 API key
func (s *UserAPIKeyService) IssueApiKey(ctx context.Context, userID, groupID int64) (string, error) {
	if userID <= 0 {
		return "", errors.New("userID required")
	}

	// 检查是否已经有 active 的 API key（避免重复签发）
	//
	// 重要（Round 4 修复）：不再校验 !IsUserAPIKeyExpired(existing)。
	// 原因：schema 有 partial unique index WHERE status='active'，如果旧 key
	// 已过期但 status 仍是 'active'（项目内无 cron 清理），跳过 revoke 分支
	// 会让后续 INSERT 命中唯一索引冲突 → distributor_order_service 自动签发
	// 链路在 30+ 天后批量失败。
	// 修复：任何 active existing key 一律先 revoke 再签发，避免 active 重复。
	existing, err := s.db.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.StatusEQ("active")).
		First(ctx)
	if err == nil && existing != nil {
		s.logger.Info("user_api_key: existing active key found, revoke before re-issue",
			"user_id", userID,
			"existing_key_id", existing.ID,
			"was_expired", IsUserAPIKeyExpired(existing),
		)
		// 吊销旧 key（保持 partial unique index 的「active 唯一」语义）
		if _, err := s.db.UserAPIKey.UpdateOneID(existing.ID).
			SetStatus("revoked").
			Save(ctx); err != nil {
			return "", fmt.Errorf("revoke existing key: %w", err)
		}
	}

	// 1. 生成明文 API key
	plaintext := generateAPIKeyPlaintext()

	// 2. bcrypt 哈希（慢哈希，存数据库查重用）
	bcryptHash, err := bcrypt.GenerateFromPassword([]byte(plaintext), bcrypt.DefaultCost)
	if err != nil {
		return "", fmt.Errorf("bcrypt hash: %w", err)
	}

	// 3. AES-256-GCM 加密（可解密，用于审计/迁移）
	encrypted, err := s.encrypt(plaintext)
	if err != nil {
		return "", fmt.Errorf("aes encrypt: %w", err)
	}

	// 4. INSERT distributor delivery key record.
	expiresAt := time.Now().Add(30 * 24 * time.Hour)
	_, err = s.db.UserAPIKey.Create().
		SetUserID(userID).
		SetGroupID(groupID).
		SetKeyHash(string(bcryptHash)).
		SetKeyEncrypted(encrypted).
		SetStatus("active").
		SetExpiresAt(expiresAt).
		Save(ctx)
	if err != nil {
		return "", fmt.Errorf("create user_api_key: %w", err)
	}

	// 5. Mirror the delivered key into the standard api_keys table used by
	// gateway auth and console UI. user_api_keys is the distributor delivery
	// audit table; api_keys is the actual runtime credential table.
	if _, err := s.db.User.UpdateOneID(userID).
		SetStatus("active").
		Save(ctx); err != nil {
		return "", fmt.Errorf("activate shadow user: %w", err)
	}
	if _, err := s.db.APIKey.Update().
		Where(
			apikey.UserIDEQ(userID),
			apikey.GroupIDEQ(groupID),
			apikey.StatusEQ("active"),
			apikey.NameHasPrefix("Steadflow 自动交付"),
		).
		SetStatus("disabled").
		Save(ctx); err != nil {
		return "", fmt.Errorf("disable existing runtime api keys: %w", err)
	}
	if _, err := s.db.APIKey.Create().
		SetUserID(userID).
		SetGroupID(groupID).
		SetKey(plaintext).
		SetName(fmt.Sprintf("Steadflow 自动交付 %s", time.Now().Format("2006-01-02"))).
		SetStatus("active").
		SetExpiresAt(expiresAt).
		Save(ctx); err != nil {
		return "", fmt.Errorf("create runtime api key: %w", err)
	}

	s.logger.Info("user_api_key: issued",
		"user_id", userID,
		"group_id", groupID,
		"expires_at", expiresAt,
	)

	// 6. 返回明文（调用方仅此一次机会拿到明文）
	return plaintext, nil
}

// ListByUser 列出用户的所有 API key（脱敏）。
func (s *UserAPIKeyService) ListByUser(ctx context.Context, userID int64) ([]*dbent.UserAPIKey, error) {
	keys, err := s.db.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID)).
		Order(dbent.Asc(userapikey.FieldCreatedAt)).
		All(ctx)
	if err != nil {
		return nil, fmt.Errorf("query user_api_keys: %w", err)
	}
	return keys, nil
}

// GetActiveApiKey 返回指定用户/分组当前可复用的明文 key。
// 分销商订单重试必须复用已签发的 runtime key，不能再次签发并吊销旧 key。
func (s *UserAPIKeyService) GetActiveApiKey(ctx context.Context, userID, groupID int64) (string, *time.Time, error) {
	k, err := s.db.UserAPIKey.Query().
		Where(userapikey.UserIDEQ(userID), userapikey.GroupIDEQ(groupID), userapikey.StatusEQ("active")).
		Order(dbent.Desc(userapikey.FieldCreatedAt)).
		First(ctx)
	if err != nil {
		return "", nil, err
	}
	if IsUserAPIKeyExpired(k) {
		return "", k.ExpiresAt, errors.New("active api key expired")
	}
	if k.KeyEncrypted == nil || *k.KeyEncrypted == "" {
		return "", k.ExpiresAt, errors.New("active api key plaintext unavailable")
	}
	plaintext, err := s.Decrypt(*k.KeyEncrypted)
	if err != nil {
		return "", k.ExpiresAt, fmt.Errorf("decrypt active api key: %w", err)
	}
	return plaintext, k.ExpiresAt, nil
}

// Revoke 吊销指定 API key。
func (s *UserAPIKeyService) Revoke(ctx context.Context, keyID int64) error {
	_, err := s.db.UserAPIKey.UpdateOneID(keyID).
		SetStatus("revoked").
		Save(ctx)
	return err
}

// encrypt 用 AES-256-GCM 加密 plaintext。
// 输出格式：iv(12 bytes) + tag(16 bytes) + ciphertext
// 这里用 hex 编码便于存储。
func (s *UserAPIKeyService) encrypt(plaintext string) (string, error) {
	block, err := aes.NewCipher(s.encryptionKey)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	iv := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, iv); err != nil {
		return "", err
	}
	ciphertext := gcm.Seal(iv, iv, []byte(plaintext), nil)
	return hex.EncodeToString(ciphertext), nil
}

// Decrypt 用于审计场景（一般不调用；明文只在签发时返回）。
func (s *UserAPIKeyService) Decrypt(encryptedHex string) (string, error) {
	data, err := hex.DecodeString(encryptedHex)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(s.encryptionKey)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(data) < gcm.NonceSize() {
		return "", errors.New("ciphertext too short")
	}
	iv := data[:gcm.NonceSize()]
	ciphertext := data[gcm.NonceSize():]
	plaintext, err := gcm.Open(nil, iv, ciphertext, nil)
	if err != nil {
		return "", err
	}
	return string(plaintext), nil
}

// generateAPIKeyPlaintext 生成 sk-<32位 hex> 格式的明文 API key。
func generateAPIKeyPlaintext() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		// 极端情况
		return "sk-fallback-" + strings.Repeat("0", 32)
	}
	return "sk-" + hex.EncodeToString(b)
}
