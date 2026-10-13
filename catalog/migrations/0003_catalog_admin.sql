-- MA-156 (MA-42): admin write API for products/categories.
--
-- Additive, all nullable or defaulted — no backfill migration needed for
-- existing seeded rows (MA-156 §7).
--
-- The SQLAlchemy Core tables in src/adapters/product_repository.py must
-- stay column-for-column compatible with this, same hand-sync discipline
--0001/0002 already document.

ALTER TABLE products ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE products ADD COLUMN hsn_code  VARCHAR(16);
ALTER TABLE products ADD COLUMN tax_rate  NUMERIC(5, 2);

-- New table. Same INTENT as Inventory's inventory_audit_log (MA-119 §7)
-- — an immutable, admin-attributed audit trail — but a genuinely
-- different SHAPE: a product create/edit can change any of a dozen
-- heterogeneous fields at once, so this table uses a generic
-- action/previous_value/new_value JSONB diff instead of Inventory's
-- typed-integer columns (MA-156 §7/§11 Risk 5).
CREATE TABLE catalog_audit_log (
    id              VARCHAR(64) PRIMARY KEY,
    product_id      VARCHAR(64) NOT NULL REFERENCES products(id),
    admin_id        VARCHAR(64) NOT NULL,
    action          VARCHAR(16) NOT NULL,   -- 'CREATE' | 'UPDATE'
    previous_value  JSONB,
    new_value       JSONB,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_catalog_audit_log_product_id ON catalog_audit_log (product_id);
