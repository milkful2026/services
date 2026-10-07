-- MA-154 (MA-32): customer order cancellation. Applies on top of 0003_sweep.sql.
-- The SQLAlchemy Core tables in src/adapters/order_repository.py must
-- stay column-for-column compatible with this.
--
-- A customer cancel sets status CANCELLED with failure_reason
-- CUSTOMER_CANCELLED (failure_reason is free text, so no constraint change),
-- plus the columns below. refund_state tracks the refund to the Wallet
-- (MA-153): PENDING until Wallet confirms, then REFUNDED; NOT_REQUIRED for a
-- ₹0 order or when Wallet holds no debit. Existing rows are untouched (all NULL).

ALTER TABLE orders ADD COLUMN cancel_reason TEXT
    CHECK (cancel_reason IN ('ORDERED_BY_MISTAKE', 'NOT_HOME', 'CHANGED_MIND', 'OTHER'));
ALTER TABLE orders ADD COLUMN cancelled_at  TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN refund_state  TEXT
    CHECK (refund_state IN ('PENDING', 'REFUNDED', 'NOT_REQUIRED'));
ALTER TABLE orders ADD COLUMN refunded_at   TIMESTAMPTZ;

-- The sweep's refund pass (MA-154 FR-5) only ever reads the PENDING few.
CREATE INDEX orders_refund_pending
    ON orders (cancelled_at) WHERE refund_state = 'PENDING';
