-- MA-136 (MA-34) — cart checkout. Production-authoritative (Postgres/
-- Aurora); src/adapters/order_repository.py's SQLAlchemy tables must stay
-- column-for-column compatible with this.
--
-- `orders` gains one-time, multi-line CHECKOUT orders alongside MA-132's
-- SUBSCRIPTION orders. Additive apart from relaxing three NOT NULLs that a
-- CHECKOUT order can't satisfy; existing rows backfill to 'SUBSCRIPTION'
-- via the column default, and the CHECK below keeps every SUBSCRIPTION
-- row exactly as constrained as before.

ALTER TABLE orders ALTER COLUMN subscription_id DROP NOT NULL;
ALTER TABLE orders ALTER COLUMN product_id DROP NOT NULL;
ALTER TABLE orders ALTER COLUMN quantity DROP NOT NULL;
ALTER TABLE orders ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'SUBSCRIPTION';
-- One order per checkout, ever — the second of the four double-charge
-- guards (MA-136 §5).
ALTER TABLE orders ADD COLUMN checkout_id VARCHAR(64) UNIQUE;
ALTER TABLE orders ADD CONSTRAINT orders_source_shape CHECK (
    (source = 'SUBSCRIPTION'
        AND subscription_id IS NOT NULL
        AND product_id IS NOT NULL
        AND quantity IS NOT NULL)
    OR (source = 'CHECKOUT'
        AND checkout_id IS NOT NULL
        AND subscription_id IS NULL)
);
-- UNIQUE (subscription_id, delivery_date) is unchanged: Postgres treats
-- NULLs as distinct, so CHECKOUT orders (subscription_id NULL) never
-- collide with each other or with subscription orders.

CREATE TABLE order_items (
    order_id   VARCHAR(64) NOT NULL REFERENCES orders (id),
    line_no    INTEGER     NOT NULL,
    product_id VARCHAR(64) NOT NULL,
    quantity   INTEGER     NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, line_no)
);

-- The resumable checkout record (MA-136 FR-2/FR-4). One row per
-- (user, Idempotency-Key); `step` is where a retried request resumes.
CREATE TABLE checkouts (
    id                   VARCHAR(64)  PRIMARY KEY,
    user_id              VARCHAR(64)  NOT NULL,
    idempotency_key      VARCHAR(128) NOT NULL,
    cart_version         INTEGER      NOT NULL,
    -- IN_PROGRESS | COMPLETED | PAYMENT_FAILED
    status               VARCHAR(16)  NOT NULL,
    -- STARTED | PAID | SUBSCRIPTIONS_DONE
    step                 VARCHAR(24)  NOT NULL,
    lines                JSONB        NOT NULL,
    pay_now_paise        BIGINT       NOT NULL CHECK (pay_now_paise >= 0),
    delivery_date        DATE         NOT NULL,
    order_id             VARCHAR(64),
    subscription_results JSONB        NOT NULL DEFAULT '[]',
    -- Stored FR-9 body (or PAYMENT_FAILED error) for same-key replay.
    result               JSONB,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);

-- At most one live checkout per user (MA-136 §5 concurrency NFR).
CREATE UNIQUE INDEX checkouts_one_live_per_user
    ON checkouts (user_id) WHERE status = 'IN_PROGRESS';
