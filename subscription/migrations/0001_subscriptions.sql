-- Production-authoritative schema (Postgres/Aurora `subscription` cluster).
-- The SQLAlchemy Core tables in src/adapters/subscription_repository.py
-- must stay column-for-column compatible with this.
--
-- First-ever migration for this service (MA-131) — no prior deployed
-- schema, created correct from the start.

CREATE TABLE subscriptions (
    id              VARCHAR(64) PRIMARY KEY,
    user_id         VARCHAR(64) NOT NULL,
    product_id      VARCHAR(64) NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    schedule        JSONB NOT NULL,             -- {type, daysOfWeek}
    slot_id         VARCHAR(64) NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | PAUSED | STOPPED
    start_date      DATE NOT NULL,
    pause_from      DATE,
    pause_until     DATE,
    pending_edit    JSONB,                      -- {quantity, schedule, effectiveFrom} | NULL
    -- Idempotency: same pattern as Payment Service's `payments` table
    -- (POST /payments) — a retried create for the same key returns the
    -- original row, never a second subscription.
    idempotency_key VARCHAR(128) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);

-- Append-only skip history — never deleted, so GET /subscriptions/{id}
-- can show "skipped" distinctly from "delivered"/"upcoming" (MA-131 FR-5).
CREATE TABLE subscription_skips (
    id             BIGSERIAL PRIMARY KEY,
    subscription_id VARCHAR(64) NOT NULL REFERENCES subscriptions(id),
    skipped_date   DATE NOT NULL
);

CREATE INDEX subscription_skips_subscription_idx ON subscription_skips (subscription_id);

-- The Daily Run's own idempotency guard (MA-131 FR-8): UNIQUE makes a
-- retried/duplicate Scheduler invocation for the same cut-off a no-op
-- per subscription, and also backs the same-day emission at create time
-- (FR-1), which records into this same table so the Daily Run never
-- double-emits for that date.
CREATE TABLE subscription_run_log (
    id              BIGSERIAL PRIMARY KEY,
    subscription_id VARCHAR(64) NOT NULL REFERENCES subscriptions(id),
    delivery_date   DATE NOT NULL,
    UNIQUE (subscription_id, delivery_date)
);

-- Transactional outbox — SubscriptionOrderDue is written here in the
-- same DB transaction as the subscription/run-log change, then published
-- to EventBridge by a separate poller (never a direct PutEvents inside a
-- request transaction).
CREATE TABLE outbox (
    id           BIGSERIAL PRIMARY KEY,
    aggregate_id VARCHAR(64) NOT NULL,   -- subscription id
    event_type   VARCHAR(48) NOT NULL,   -- SubscriptionOrderDue
    payload      JSONB NOT NULL,         -- the full EventBridge 'detail'
    published_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
