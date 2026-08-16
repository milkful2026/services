# Catalog Service

Product & category data + search/filter/sort API for the Flutter app's catalog screen.
Implements backend story [MA-94](https://milkfuldairyindia.atlassian.net/browse/MA-94) (blocks
[MA-22](https://milkfuldairyindia.atlassian.net/browse/MA-22)), per specs
`specs/services/tasks/MA/MA-94/MA-116.md` (data API) and
`specs/services/tasks/MA/MA-94/MA-117.md` (search).

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| GET | `/categories` | MA-116 FR-3 | Cognito JWT (any authenticated user) |
| GET | `/products?categoryId=` | MA-116 FR-1 | Cognito JWT |
| GET | `/products/{id}` | MA-116 FR-2 | Cognito JWT |
| GET | `/search?q=&filters=&sort=` | MA-117 FR-1 | Cognito JWT |

`/categories`'s `data` is a bare JSON array (matches the mobile client's `ApiClient.requestList`
convention — see `handlers/dto.py`'s `success_list_envelope`); every other endpoint wraps its
list in `{"products": [...]}`. `filters` is a repeated query param, one per facet:
`category:{id}`, `price:{min}-{max}`, `veg:true`, `organic:true`.

Real Cognito-JWT enforcement (an API Gateway authorizer) isn't wired in this local-dev pass —
matches every other service in this repo's own local-dev shim, which decodes-not-verifies.

## Architecture decisions flagged for review

1. **`GET /search` runs directly against Postgres, not real OpenSearch.** MA-117's own spec names
   OpenSearch as the single biggest implementation risk in that spec set — no local-dev emulation
   precedent exists for it anywhere in this repo (identity-auth/user/inventory use
   Cognito/DynamoDB/SQS/EventBridge via moto, and Postgres/Redis directly — nothing OpenSearch-like).
   This implementation satisfies MA-117's documented *contract* (same request params, same response
   shape as MA-116's other endpoints) via `ILIKE`/`WHERE`/`ORDER BY` on the same `products` table
   MA-116 owns, so the mobile client and this service agree on behavior regardless of what's
   underneath. Swapping to real OpenSearch later is a repository-adapter change, not a contract
   change.
   - `sort=newest` orders by `created_at desc` (added in migration `0001`/`0002` review follow-up —
     see decision #4 below for the schema fix that closed this gap).
2. **`is_veg`/`is_organic` live on `products`, not a separate attributes table.** Two independent
   booleons (a product can be organic-but-not-veg-labeled, or vice versa, in a dairy context) —
   added jointly with MA-117 during SDD review after that spec flagged the schema gap.
3. **B2B pricing is schema-ready, not caller-aware.** `products.price_b2b` exists and is nullable;
   every endpoint always returns `price_b2c` in the `price` field (MA-116 FR-4) until a caller can
   signal B2B intent — MA-116's own Open Question Q2, still unresolved, no B2B-aware caller exists
   yet (MA-21's mobile spec also explicitly limited B2B to "indicator only").
4. **`StockChanged` idempotency via a stored `last_stock_event_id` + `last_stock_event_at`**, not a
   separate dedup table. `last_stock_event_id` alone only catches an exact redelivery (same
   `eventId` twice) — since SQS here is a standard, non-FIFO queue, a *distinct* event can still
   arrive out of order, so `apply_stock_change` also compares the incoming event's own `occurredAt`
   against the last-applied one's and drops anything not strictly newer. Still just two fields on
   the product row, not a general-purpose event log.
5. **`created_at`/`updated_at` on both tables, `onupdate=func.now()` on `products.updated_at`** —
   present in the SQLAlchemy Core mapping (not just the migration) so they're actually queryable;
   `updated_at` bumps automatically on any `products` UPDATE via SQLAlchemy Core's own `onupdate`,
   no explicit `SET` needed in `apply_stock_change`'s statement.
6. **Single Fargate deployable** handles both the HTTP API and the `StockChanged` SQS consumer (a
   background thread in the same process, `src/main.py`) — mirrors Inventory's own
   `src/main.py` structure exactly (same "nothing here calls for a two-deployable split" reasoning).
   A DB error applying one event is caught and left in-queue for SQS's own redelivery/DLQ rather
   than killing the consumer thread; only truly unexpected exceptions reach `main.py`'s top-level
   handler, which flips `/healthz` to 503.
7. **No CDK/infra stack.** Unlike `inventory/infra/`, this pass has no `infra/` directory — building
   one wasn't needed to prove out and locally run/test the service, and an untested CDK stack isn't
   worth the surface area. Flagged as follow-up work, not an oversight.

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate  # or .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
pytest                          # full suite: domain + adapters + handlers, no AWS/DB needed

# from services/local-dev/, after docker compose up -d + bootstrap.py + apply_migrations.py:
python seed_catalog_products.py
cd ../catalog && python src/main.py   # :8003
```

See `services/local-dev/README.md`'s "Exercising the catalog" section for `curl` walkthroughs,
including how to manually publish a `StockChanged` event (no real producer exists yet — Inventory's
reserve/commit/release, MA-95/MA-118, is spec'd but not implemented).

## Testing approach

Fully offline — **no AWS credentials, no Docker, no real Postgres**:

- SQLite in-memory (`StaticPool`, `check_same_thread=False` — required because FastAPI's
  `TestClient` runs sync endpoints in a worker thread pool) stands in for Aurora, mirroring
  `inventory`'s own documented fidelity gap and reasoning exactly.
- `moto[sqs]` stands in for the `stock-changed` queue.
- `httpx`/FastAPI `TestClient` exercises the real HTTP layer without a running server.
- Verified end-to-end against a real local stack too, not just unit tests: `docker compose up`,
  `bootstrap.py`, `apply_migrations.py`, `seed_catalog_products.py`, the service actually running
  on `:8003`, and a real `StockChanged` event published to the local `moto_server` SQS queue and
  observed taking effect via `GET /products/{id}`.

## What still needs a human

- Real Cognito JWT verification at the API Gateway layer (not built in any service's local-dev
  shim yet — see `services/local-dev/README.md`'s own "JWT claims aren't verified" known gap).
- A real OpenSearch index, if/when `GET /search`'s current Postgres-direct implementation stops
  being good enough (flagged decision #1).
- CDK/infra stack (flagged decision #7) — provisioning, `cdk bootstrap`/`deploy`, ECR image
  pipeline, none of which exist for this service yet.
- Confirming Inventory's real `StockChanged` publisher (once MA-95/MA-118 is implemented) actually
  matches the payload contract this service's consumer expects (`FR-5`/`FR-6` in the two specs) —
  proven so far only against a manually-published test event, not a real producer. In particular,
  whether it sends `occurredAt` as the event's true origination time (not, say, a publish-retry
  time) — `apply_stock_change`'s ordering check (decision #4) depends on that being accurate.
- A retry-with-backoff for `apply_stock_change`'s transient DB failures (decision #6) — currently a
  failed apply is just left for SQS's own redelivery/DLQ, no in-process retry.

## Deferred / tech debt

- Admin/write API for products & categories (create/edit/deactivate, bulk import) — MA-42's
  territory, out of scope for MA-94/this service per its own spec's explicit Scope section.
- Image upload/CDN pipeline — this service assumes `image_url` values already exist on product
  records; nothing here populates them (seed data intentionally ships with `image_url = NULL`).
