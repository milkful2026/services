-- MA-139 §7 — Customer Account Status. Additive migration: NOT NULL with a
-- constant DEFAULT on `status` needs no table rewrite/full scan (same
-- Postgres 11+ fast-path 0002_add_account_type.sql's own comment already
-- documents); the CHECK constraint is added NOT VALID + VALIDATE
-- CONSTRAINT separately so it doesn't take a long-held ACCESS EXCLUSIVE
-- lock on `users` either.

ALTER TABLE users
  ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'Active';

ALTER TABLE users
  ADD CONSTRAINT ck_users_status
    CHECK (status IN ('Active', 'Suspended', 'Deactivated')) NOT VALID;

ALTER TABLE users
  VALIDATE CONSTRAINT ck_users_status;

ALTER TABLE users
  ADD COLUMN status_reason VARCHAR(500);

ALTER TABLE users
  ADD COLUMN status_effective_from DATE;

-- NULL unless status = 'Suspended' (spec §7) — not enforced as a DB
-- constraint (would need a CHECK referencing another column's value,
-- fragile across the three-way status/suspended_until/status_reason
-- relationship this spec's transitions define); the domain layer
-- (customer_status_service.py) is the source of truth for that
-- invariant, consistent with the spec's own "enforced at the domain
-- layer, not just a DB constraint" instruction for the reason/until
-- validation rules.
ALTER TABLE users
  ADD COLUMN suspended_until DATE;

-- Indexes named per spec §5 NFR-Performance ("indexed on status, and on
-- name/mobile/email via existing lookup patterns") — list_customers'
-- free-text search still does a case-insensitive LIKE scan (same
-- "low-hundreds/thousands scale, no trigram index needed" reasoning
-- identity-auth's own idx_admin_user_email_lower/idx_admin_user_name_lower
-- already established), but filtering by status alone is now indexed.
CREATE INDEX idx_users_status ON users (status);

-- MA-139 §7 — durable status-change audit trail (who/what/when/why).
-- Application-generated UUID primary key (Python uuid4, stored as text),
-- same portability reasoning as every other table in this migration set
-- (see 0001_users_addresses_consents.sql's own header comment).
CREATE TABLE user_status_history (
    id                VARCHAR(36) PRIMARY KEY,
    user_id           VARCHAR(36) NOT NULL REFERENCES users(id),
    previous_status   VARCHAR(16),
    new_status        VARCHAR(16) NOT NULL,
    reason            VARCHAR(500),
    effective_from    DATE,
    actor_admin_id    VARCHAR(128) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_user_status_history_user_id ON user_status_history (user_id);

-- Supports list_customers' `lastStatusChangeAt` (MAX(created_at) per
-- user_id, spec §4 FR-1) without a full per-account scan of this table.
CREATE INDEX idx_user_status_history_user_id_created_at
  ON user_status_history (user_id, created_at DESC);
