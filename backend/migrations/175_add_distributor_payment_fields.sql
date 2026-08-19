-- Distributor order linkage. All fields are nullable for existing orders.
ALTER TABLE payment_orders
    ADD COLUMN IF NOT EXISTS distributor_email VARCHAR(255),
    ADD COLUMN IF NOT EXISTS external_user_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS created_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS distributor_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_payment_orders_distributor_email
    ON payment_orders(distributor_email);
CREATE INDEX IF NOT EXISTS idx_payment_orders_distributor_id
    ON payment_orders(distributor_id);
