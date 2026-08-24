# Cart Service

DynamoDB-backed cart line items — add/replace/remove, optimistic concurrency across
devices/sessions, Idempotency-Key deduplication, and a server-side wallet-balance gate for
subscription line items. Implements
[MA-96](https://milkfuldairyindia.atlassian.net/browse/MA-96) (spec
`specs/services/tasks/MA/MA-96/MA-121.md`).

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| GET | `/cart` | FR-1 | Cognito JWT |
| POST | `/cart/items` | FR-2 | Cognito JWT |
| PUT | `/cart` | FR-3 | Cognito JWT |
| DELETE | `/cart/items/{id}` | FR-4 | Cognito JWT |

## Architecture decisions flagged for review

1. **Stock re-validation calls Catalog Service, not Inventory Service.** MA-121 §8 assumed
   Inventory Service would own (or gain) a per-product stock-quantity lookup. Confirmed by reading
   `services/inventory/src/domain/models.py` directly: Inventory has no stock-quantity concept at
   all — only serviceability zones/slots. Stock quantity is Catalog Service's data (MA-120 §7's own
   `available_quantity` addition, itself still unimplemented as of this service's own initial
   build — see Known Gaps). This service calls Catalog, matching MA-120's own correct analysis
   rather than MA-121's independent, incorrect assumption.
2. **Outbox via DynamoDB `transact_write_items`, single table.** Every mutating write
   (`add_item`/`replace_cart`/`delete_item`) writes its domain row(s), an `OUTBOX#{eventId}` row,
   and increments the per-user `META` row's `cartVersion` in one atomic transaction — this table's
   equivalent of the Postgres same-transaction outbox-insert pattern `user`'s own service uses.
3. **`Idempotency-Key` dedup is a conditional DynamoDB write, not an application-level check.**
   `add_item` writes an `IDEMPOTENCY#{key}` row with a `attribute_not_exists` condition in the same
   transaction as the line-item write; a condition failure means "already processed," not an error —
   the handler re-reads and returns the original result.

## Known Gaps (flagged, not silently worked around)

- **Catalog Service's `available_quantity` field (MA-120 §7) doesn't exist yet** — `add_item`/
  `replace_cart`'s stock check degrades to the same "unknown, not unbounded" treatment the mobile
  app uses (MA-23 impl plan §4C) until it lands.
- **Pricing & Offer Service (MA-101) doesn't exist yet** — `GET /cart` (which unconditionally needs
  a live quote per FR-1) fails closed with `PricingUnavailableError` until it does. This is the
  correct, spec'd failure mode, not a bug in this service.
- **Wallet Service (MA-100) doesn't exist at all** — not even spec'd. FR-6's wallet gate fails
  closed with `WalletCheckUnavailableError` for every subscription-frequency write until it exists.

## Local development

See `services/local-dev/README.md` for the full local stack. This service listens on `:8004`.

```
cd cart && python run_local.py
cd cart && python run_local_outbox_publisher.py   # drains OUTBOX# rows to EventBridge
```

## Tests

```
pytest
```
