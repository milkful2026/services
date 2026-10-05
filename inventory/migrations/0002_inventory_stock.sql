-- Inventory — Reserve/Commit/Release, Batch & Expiry, Read API (MA-118 §7).
-- Additive to 0001_serviceability_zones.sql; same database (milkful_inventory).
--
-- Application-generated UUIDs (Python uuid4, stored as text), not DB-side
-- gen_random_uuid() defaults, so the SQLAlchemy Core tables in
-- src/adapters/stock_repository.py stay portable to the SQLite test
-- double used offline (same documented fidelity-gap convention as
-- `user`/`identity-auth`/`wallet`).
--
-- `reserved` on `stock` is a derived/cached column (MA-118 §7's own
-- comment: "source of truth is the reservations table's active rows,
-- kept in sync transactionally") — every write to it happens inside the
-- same transaction, under the same row lock, as the reservations-table
-- write that justifies it (reserve/commit/release, MA-119's adjust,
-- MA-150's receive all take `SELECT ... FOR UPDATE` on this row first,
-- mirroring wallet/src/adapters/wallet_repository.py's `debit_for_order`
-- lock-check-write shape).

CREATE TABLE stock (
    product_id          VARCHAR(64) PRIMARY KEY,
    on_hand              INTEGER NOT NULL DEFAULT 0,
    reserved             INTEGER NOT NULL DEFAULT 0,
    low_stock_threshold   INTEGER NOT NULL DEFAULT 10,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_stock_on_hand_nonneg CHECK (on_hand >= 0),
    CONSTRAINT ck_stock_reserved_nonneg CHECK (reserved >= 0)
);

CREATE TABLE stock_batches (
    id              VARCHAR(36) PRIMARY KEY,
    product_id      VARCHAR(64) NOT NULL REFERENCES stock(product_id),
    quantity        INTEGER NOT NULL,
    expiry_date     DATE,
    available_from  DATE,                 -- NULL = available now; set = scheduled future batch
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_stock_batches_quantity_nonneg CHECK (quantity >= 0)
);

-- FIFO-by-expiry consumption (FR-2) and oldest-expiry-first batch listing
-- (MA-150 FR-2) both order by this.
CREATE INDEX idx_stock_batches_product_expiry ON stock_batches (product_id, expiry_date);

CREATE TABLE reservations (
    id           VARCHAR(36) PRIMARY KEY,
    product_id   VARCHAR(64) NOT NULL REFERENCES stock(product_id),
    order_ref    TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    status       VARCHAR(16) NOT NULL,     -- RESERVED | COMMITTED | RELEASED
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_reservations_quantity_pos CHECK (quantity > 0),
    CONSTRAINT ck_reservations_status CHECK (status IN ('RESERVED', 'COMMITTED', 'RELEASED'))
);

-- FR-2's reserve idempotency, enforced at the DB layer, not just in
-- application logic — a race between two retries of the same
-- (productId, orderId) can't both insert.
CREATE UNIQUE INDEX reservations_product_order_uq ON reservations (product_id, order_ref);

-- The TTL sweep (a "SELECT ... FOR UPDATE SKIP LOCKED"-style scan, per
-- §5 NFR Reliability) scans exactly this shape: still RESERVED, past
-- expiry.
CREATE INDEX idx_reservations_status_expires ON reservations (status, expires_at)
    WHERE status = 'RESERVED';

-- OrderCancelled consumption (FR-5) looks up every reservation for an
-- order_ref across products (an order can hold more than one product's
-- reservation) — not covered by the product_id-prefixed unique index
-- above.
CREATE INDEX idx_reservations_order_ref ON reservations (order_ref);
