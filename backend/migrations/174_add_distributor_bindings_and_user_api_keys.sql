-- Distributor credentials and user API-key delivery audit records.
CREATE TABLE IF NOT EXISTS distributor_bindings (
    id BIGSERIAL PRIMARY KEY,
    distributor_id VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    contact_email VARCHAR(255),
    notes TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_distributor_bindings_enabled ON distributor_bindings(enabled);
CREATE INDEX IF NOT EXISTS idx_distributor_bindings_expires_at ON distributor_bindings(expires_at);

CREATE TABLE IF NOT EXISTS user_api_keys (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    group_id BIGINT NOT NULL,
    key_hash VARCHAR(128) NOT NULL,
    key_encrypted TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    issued_by VARCHAR(64),
    issued_by_id VARCHAR(128),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_id ON user_api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_group_id ON user_api_keys(group_id);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_status ON user_api_keys(status);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_expires_at ON user_api_keys(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_api_keys_user_group_active
    ON user_api_keys(user_id, group_id)
    WHERE status = 'active';
