-- New accounts only: bcrypt login hash + encrypted order-scoped delivery credential.
-- No backfill or password reset for existing users.
ALTER TABLE users ADD COLUMN IF NOT EXISTS distributor_login_encrypted text;
