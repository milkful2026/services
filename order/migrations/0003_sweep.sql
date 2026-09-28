-- MA-138 / MA-143: reconciliation sweep. Applies on top of 0002_checkout.sql.
-- The SQLAlchemy Core tables in src/adapters/order_repository.py must
-- stay column-for-column compatible with this.
--
-- A lease (`claimed_until` + `claim_owner`) lets exactly one worker — the
-- sweep, an SQS redelivery, or (MA-144) a customer checkout request —
-- work on a record at a time without holding a DB transaction across an
-- HTTP call. `sweep_attempts` counts failed sweep runs only; at
-- ORDER_SWEEP_MAX_ATTEMPTS the record is escalated to NEEDS_ATTENTION.
-- Status columns are VARCHAR(16) with no CHECK constraint, so the new
-- NEEDS_ATTENTION / CANCELLED values need no constraint change.

ALTER TABLE orders    ADD COLUMN sweep_attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orders    ADD COLUMN claimed_until    TIMESTAMPTZ;
ALTER TABLE orders    ADD COLUMN claim_owner      VARCHAR(64);
ALTER TABLE orders    ADD COLUMN last_sweep_error TEXT;

ALTER TABLE checkouts ADD COLUMN sweep_attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE checkouts ADD COLUMN claimed_until    TIMESTAMPTZ;
ALTER TABLE checkouts ADD COLUMN claim_owner      VARCHAR(64);
ALTER TABLE checkouts ADD COLUMN last_sweep_error TEXT;

-- Partial indexes keep each sweep query to the handful of live rows.
CREATE INDEX orders_sweep_subscription
    ON orders (created_at) WHERE status = 'CREATED' AND source = 'SUBSCRIPTION';
CREATE INDEX checkouts_sweep_in_progress
    ON checkouts (updated_at) WHERE status = 'IN_PROGRESS';
