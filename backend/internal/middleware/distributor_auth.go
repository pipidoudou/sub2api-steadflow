// Package middleware 提供 HTTP 请求处理中间件。
//
// distributor_auth.go - 分销商鉴权中间件（v2.2 §4.2）
//
// 实现要点：
//   - 校验 X-Distributor-Token header
//   - token 格式：dist_<32位随机>
//   - 校验通过后，将 distributor 信息写入 ctx，供下游 handler 使用
//   - 校验失败返回 401
package middleware

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"log/slog"
	"net/http"
	"strings"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/distributorbinding"
)

// DistributorContextKey 是 ctx 中 distributor 信息的 key。
type DistributorContextKey string

const (
	// DistributorIDKey 是 distributor ID 在 ctx 中的 key。
	DistributorIDKey DistributorContextKey = "distributor_id"
	// DistributorNameKey 是 distributor name 在 ctx 中的 key。
	DistributorNameKey DistributorContextKey = "distributor_name"
)

// DistributorAuthConfig 是中间件的配置。
type DistributorAuthConfig struct {
	// DB 是 ent client（用于查询 distributor_bindings 表）。
	DB *dbent.Client
	// Logger 用于记录认证失败事件。
	Logger *slog.Logger
}

// DistributorAuth 返回 Gin/Fastify 风格的中间件。
// 实现 http.Handler 中间件签名。
//
// 用法：
//
//	router.Use(middleware.DistributorAuth(cfg))
//	router.GET("/api/v1/distributor/orders", handler.ListOrders)
func DistributorAuth(cfg DistributorAuthConfig) func(http.Handler) http.Handler {
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	db := cfg.DB

	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := extractDistributorToken(r)
			if token == "" {
				logger.Warn("distributor auth: missing token",
					"path", r.URL.Path,
					"ip", clientIP(r),
				)
				writeUnauthorized(w, "missing X-Distributor-Token header")
				return
			}

			if !strings.HasPrefix(token, "dist_") {
				logger.Warn("distributor auth: invalid token format",
					"path", r.URL.Path,
					"ip", clientIP(r),
				)
				writeUnauthorized(w, "invalid token format")
				return
			}

			// 查询 distributor_bindings 表
			// 注：distributor_bindings 表是本次 v2.2 新增，schema 见
			// ent/migrate/migrations/20260708_distributor_and_apikeys.sql
			tokenHash := hashToken(token)
			binding, err := db.DistributorBinding.Query().
				Where(distributorbinding.TokenHash(tokenHash)).
				Only(r.Context())
			if dbent.IsNotFound(err) {
				binding, err = db.DistributorBinding.Query().
					Where(distributorbinding.TokenHash(token)).
					Only(r.Context())
				if err == nil {
					_, _ = db.DistributorBinding.UpdateOne(binding).
						SetTokenHash(tokenHash).
						Save(r.Context())
					binding.TokenHash = tokenHash
				}
			}
			if err != nil {
				if dbent.IsNotFound(err) {
					logger.Warn("distributor auth: token not found",
						"path", r.URL.Path,
						"ip", clientIP(r),
					)
					writeUnauthorized(w, "invalid token")
					return
				}
				logger.Error("distributor auth: db error",
					"path", r.URL.Path,
					"err", err.Error(),
				)
				writeInternalError(w)
				return
			}

			if !binding.Enabled {
				logger.Warn("distributor auth: token disabled",
					"distributor_id", binding.ID,
					"path", r.URL.Path,
				)
				writeUnauthorized(w, "token disabled")
				return
			}
			if binding.ExpiresAt != nil && time.Now().After(*binding.ExpiresAt) {
				logger.Warn("distributor auth: token expired",
					"distributor_id", binding.ID,
					"path", r.URL.Path,
				)
				writeUnauthorized(w, "token expired")
				return
			}

			// 用 subtle.ConstantTimeCompare 防时序攻击（虽然 token 是唯一索引，
			// 但保留这一层防御）
			if subtle.ConstantTimeCompare([]byte(binding.TokenHash), []byte(tokenHash)) != 1 {
				writeUnauthorized(w, "invalid token")
				return
			}

			// 把 distributor 信息写入 ctx
			ctx := context.WithValue(r.Context(), DistributorIDKey, binding.ID)
			ctx = context.WithValue(ctx, DistributorNameKey, binding.Name)

			logger.Debug("distributor auth: ok",
				"distributor_id", binding.ID,
				"distributor_name", binding.Name,
				"path", r.URL.Path,
			)

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// extractDistributorToken 从 header 提取 token。
func extractDistributorToken(r *http.Request) string {
	return strings.TrimSpace(r.Header.Get("X-Distributor-Token"))
}

// hashToken 对 token 做 SHA-256 哈希（存到 distributor_bindings.token_hash）。
// 注：与 bcrypt 不同，本项目为了支持「O(1) 查询」选用 SHA-256 + token 唯一性约束。
// 真实生产环境推荐用 bcrypt 或 argon2。
func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// clientIP 取客户端 IP（仅从 X-Forwarded-For / X-Real-IP 取，部署在反代后）。
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		// 取第一个 IP（原始客户端）
		if idx := strings.Index(xff, ","); idx > 0 {
			return strings.TrimSpace(xff[:idx])
		}
		return strings.TrimSpace(xff)
	}
	if xri := r.Header.Get("X-Real-IP"); xri != "" {
		return xri
	}
	return r.RemoteAddr
}

func writeUnauthorized(w http.ResponseWriter, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(`{"ok":false,"error":"UNAUTHORIZED","message":"` + msg + `"}`))
}

func writeInternalError(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusInternalServerError)
	_, _ = w.Write([]byte(`{"ok":false,"error":"INTERNAL_ERROR"}`))
}

// GetDistributorID 辅助函数：从 ctx 取 distributor ID。
func GetDistributorID(ctx context.Context) (int64, bool) {
	v, ok := ctx.Value(DistributorIDKey).(int64)
	return v, ok
}

// GetDistributorName 辅助函数：从 ctx 取 distributor name。
func GetDistributorName(ctx context.Context) (string, bool) {
	v, ok := ctx.Value(DistributorNameKey).(string)
	return v, ok
}
