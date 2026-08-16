-- Production-authoritative schema (Postgres/Aurora). The SQLAlchemy Core
-- tables in src/adapters/product_repository.py must stay column-for-column
-- compatible with this.
--
-- is_veg/is_organic exist specifically for the veg/organic filter facet on
-- GET /search (MA-117) — added jointly with that spec during SDD review,
-- see MA-116 §7's own note.
--
-- price_b2c is always what GET /products/GET /search return today
-- (MA-116 FR-4) — price_b2b is nullable and unused until a caller can
-- signal B2B intent (MA-116's own Open Question Q2, still unresolved).

CREATE TABLE categories (
    id          VARCHAR(64) PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    icon_name   VARCHAR(64) NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id                      VARCHAR(64) PRIMARY KEY,
    category_id             VARCHAR(64) NOT NULL REFERENCES categories(id),
    name                    VARCHAR(255) NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    unit                    VARCHAR(64) NOT NULL,
    price_b2c               NUMERIC(10, 2) NOT NULL,
    price_b2b               NUMERIC(10, 2),
    image_url               TEXT,
    tag                     VARCHAR(32),
    subscription_eligible   BOOLEAN NOT NULL DEFAULT false,
    is_veg                  BOOLEAN NOT NULL DEFAULT true,
    is_organic              BOOLEAN NOT NULL DEFAULT false,
    -- Mirrored from Inventory's StockChanged event (MA-116 FR-5) — this
    -- service never computes stock itself.
    stock_state             VARCHAR(16) NOT NULL DEFAULT 'IN_STOCK',
    available_from          DATE,
    -- Idempotency guard for StockChanged consumption (MA-116 FR-5) — the
    -- last-applied event's id, so a redelivered duplicate is a no-op.
    last_stock_event_id     VARCHAR(64),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_products_category_id ON products (category_id);
CREATE INDEX idx_categories_sort_order ON categories (sort_order);
