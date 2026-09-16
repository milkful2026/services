-- Production-authoritative schema (Postgres/Aurora `payments` cluster).
-- The SQLAlchemy Core tables in src/adapters/payment_repository.py must
-- stay column-for-column compatible with this.
--
-- Money is integer paise (BIGINT) everywhere. `purpose` carries an
-- unused ORDER value from day one so a future cart-checkout slice is a
-- pure addition, not a schema change (MA-126 SS3/SS11).

CREATE TABLE payments (
    id                  VARCHAR(64) PRIMARY KEY,
    user_id             VARCHAR(64) NOT NULL,
    purpose             VARCHAR(16) NOT NULL,              -- WALLET_RECHARGE | ORDER
    amount_paise        BIGINT NOT NULL CHECK (amount_paise > 0),
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    status              VARCHAR(16) NOT NULL DEFAULT 'CREATED',  -- CREATED|CONFIRMING|CONFIRMED|FAILED
    method              VARCHAR(16),                       -- UPI | CARD | NETBANKING | WALLET | OTHER
    razorpay_order_id   TEXT UNIQUE,
    razorpay_payment_id TEXT,
    razorpay_signature  TEXT,
    idempotency_key     TEXT NOT NULL,
    failure_code        TEXT,
    failure_reason      TEXT,
    correlation_id      TEXT NOT NULL,
    captured_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);

-- Reconciliation sweep: find stale CONFIRMING / CREATED-with-order rows.
CREATE INDEX payments_status_updated_idx ON payments (status, updated_at);

-- Immutable audit of every state-affecting input (client confirm calls,
-- webhook deliveries incl. duplicates, reconciliation actions).
CREATE TABLE payment_events (
    id           BIGSERIAL PRIMARY KEY,
    payment_id   VARCHAR(64) NOT NULL REFERENCES payments(id),
    source       TEXT NOT NULL,   -- INTERNAL_CREATE | CLIENT_CONFIRM | CLIENT_CONFIRM_REJECTED
                                   -- | WEBHOOK | WEBHOOK_DUP | LATE_CAPTURE_RECOVERED | RECONCILE
    raw_payload  JSONB NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX payment_events_payment_id_idx ON payment_events (payment_id);

-- Transactional outbox — PaymentConfirmed / PaymentFailed are written
-- here in the same DB transaction as the status change, then published
-- to EventBridge by a separate poller.
CREATE TABLE outbox (
    id           BIGSERIAL PRIMARY KEY,
    aggregate_id VARCHAR(64) NOT NULL,   -- payment id
    event_type   VARCHAR(48) NOT NULL,   -- PaymentConfirmed | PaymentFailed
    payload      JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
