-- ============================================================
-- 2026-07-08 distributor_and_apikeys migration
-- 依据: 架构方案 v2.2 §4.3.4（v2.2 任务 T04c）
-- 整合 T03 (distributor_bindings) + T03b (payment_orders 3 字段) + T04b (user_api_keys)
-- 行数: ~80 行 SQL
-- ============================================================

-- 1. distributor_bindings 表 - 存储分销商 token (前缀 dist_)
CREATE TABLE IF NOT EXISTS distributor_bindings (
    id BIGSERIAL PRIMARY KEY,
    distributor_id VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    contact_email VARCHAR(255),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_distributor_bindings_enabled ON distributor_bindings(enabled);
CREATE INDEX IF NOT EXISTS idx_distributor_bindings_expires_at ON distributor_bindings(expires_at);

-- 2. payment_orders 表加 3 字段 (shadow user 兜底关联)
ALTER TABLE payment_orders
    ADD COLUMN IF NOT EXISTS distributor_email VARCHAR(255),
    ADD COLUMN IF NOT EXISTS external_user_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS created_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS distributor_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_payment_orders_distributor_email ON payment_orders(distributor_email);
CREATE INDEX IF NOT EXISTS idx_payment_orders_distributor_id ON payment_orders(distributor_id);

-- 3. user_api_keys 表 - 用户级 API key 存储
CREATE TABLE IF NOT EXISTS user_api_keys (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    group_id BIGINT NOT NULL,
    key_hash VARCHAR(128) NOT NULL,
    key_encrypted TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'active', -- active / revoked / expired
    issued_by VARCHAR(64), -- distributor / console / redeem
    issued_by_id VARCHAR(128), -- distributor_id 或 admin_id
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_id ON user_api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_status ON user_api_keys(status);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_expires_at ON user_api_keys(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_api_keys_user_group_active
    ON user_api_keys(user_id, group_id)
    WHERE status = 'active';

-- ============================================================
-- 完成
-- ============================================================

-- 后续可能需要添加的外键（暂不在本轮）：
-- ALTER TABLE payment_orders ADD CONSTRAINT fk_payment_orders_distributor
--   FOREIGN KEY (distributor_id) REFERENCES distributor_bindings(id) ON DELETE SET NULL;
-- ALTER TABLE user_api_keys ADD CONSTRAINT fk_user_api_keys_user
--   FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;