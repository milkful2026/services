-- Production-authoritative schema (Postgres/Aurora `order` cluster).
-- The SQLAlchemy Core tables in src/adapters/order_repository.py must
-- stay column-for-column compatible with this.
--
-- First-ever migration for this service (MA-132) — no prior deployed
-- schema, created correct from the start.

CREATE TABLE orders (
    -- `seq` (not `id`) backs keyset pagination on GET /orders/me — `id`
    -- is the business key (app-generated `ord_<uuid>`, not monotonic).
    seq             BIGSERIAL PRIMARY KEY,
    id              VARCHAR(64) NOT NULL UNIQUE,
    user_id         VARCHAR(64) NOT NULL,
    subscription_id VARCHAR(64) NOT NULL,
    product_id      VARCHAR(64) NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    -- Snapshotted at materialization time (MA-132 §7) — never
    -- recomputed, so a later price change never alters an existing
    -- order. >= 0, not > 0: a PAYMENT_FAILED order created before a
    -- price was ever obtained (no delivery address on file, or the
    -- product no longer exists in Catalog) is recorded with 0 — see
    -- domain/order_service.py's module docstring for the full reasoning.
    amount_paise    BIGINT NOT NULL CHECK (amount_paise >= 0),
    delivery_date   DATE NOT NULL,
    -- CREATED | CONFIRMED | PAYMENT_FAILED | FAILED
    status          VARCHAR(16) NOT NULL DEFAULT 'CREATED',
    -- INSUFFICIENT_BALANCE | WALLET_NOT_ACTIVE | PRODUCT_UNAVAILABLE | DELIVERY_ADDRESS_UNKNOWN
    failure_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ,
    -- The core correctness guarantee (MA-132 §5 NFR): exactly one order
    -- per (subscription, delivery date) ever, regardless of message
    -- redelivery count — enforced here, not just in application logic.
    UNIQUE (subscription_id, delivery_date)
);

CREATE INDEX orders_user_seq_idx ON orders (user_id, seq DESC);

-- Transactional outbox — OrderConfirmed / OrderPaymentFailed are written
-- here in the same DB transaction as the status update, then published
-- to EventBridge by a separate poller (never a direct PutEvents inside
-- the SQS-consumer transaction).
CREATE TABLE outbox (
    id           BIGSERIAL PRIMARY KEY,
    aggregate_id VARCHAR(64) NOT NULL,   -- order id
    event_type   VARCHAR(48) NOT NULL,   -- OrderConfirmed | OrderPaymentFailed
    payload      JSONB NOT NULL,         -- the full EventBridge 'detail'
    published_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
