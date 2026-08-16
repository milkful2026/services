-- Adds the StockChanged event's own occurredAt timestamp alongside the
-- existing last_stock_event_id, so apply_stock_change can detect a
-- distinct-event-id but out-of-order (stale) redelivery — event_id
-- equality alone can't catch that, since SQS (a standard, non-FIFO queue
-- here) gives no ordering guarantee.
--
-- The SQLAlchemy Core table in src/adapters/product_repository.py must
-- stay column-for-column compatible with this, same as 0001.

ALTER TABLE products ADD COLUMN last_stock_event_at TIMESTAMPTZ;
