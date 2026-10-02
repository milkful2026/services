-- Inventory — Admin Manual Adjustment & Audit Trail (MA-119 §7).
-- Additive to 0002_inventory_stock.sql; same database (milkful_inventory).
--
-- No update/delete path is exposed by the application (MA-119 FR-2) —
-- immutability is enforced by omission, not a DB trigger (sufficient,
-- per MA-119 §9, since this is an internal-only table with no direct
-- admin write access to the database).
--
-- Written by both MA-119's `PATCH /inventory` (admin adjustment) and
-- MA-150's `POST /inventory/receive` (goods receipt) via the same
-- insert-audit-row helper — a `receive()`-originated row is
-- distinguishable only by its `reason` prefix ("goods_receipt[: ...]"),
-- not a separate column (MA-150 §4 FR-4) — no schema change needed for
-- MA-150 on top of this table.

CREATE TABLE inventory_audit_log (
    id                 VARCHAR(36) PRIMARY KEY,
    product_id         VARCHAR(64) NOT NULL,
    admin_id           TEXT NOT NULL,
    previous_quantity  INTEGER NOT NULL,
    new_quantity       INTEGER NOT NULL,
    adjustment         INTEGER NOT NULL,
    reason             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- MA-150 FR-4's read endpoint: newest-first, scoped to one productId.
CREATE INDEX idx_inventory_audit_log_product_created ON inventory_audit_log (product_id, created_at DESC);
