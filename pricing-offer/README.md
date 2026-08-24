# Pricing Service

`POST /pricing/quote` — price, tax, delivery fee, and (for subscriptions) a monthly estimate for
one or more line items. Implements the request/response contract of backend story
[MA-101](https://milkfuldairyindia.atlassian.net/browse/MA-101), per spec
`specs/services/tasks/MA/MA-101/MA-122.md`, and unblocks the mobile app's MA-23 screen
(`ProductConfigScreen`), whose "Add to Cart"/"Subscribe Now" button stays disabled until a quote
loads successfully.

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| POST | `/pricing/quote` | MA-122 FR-1 | none in this local-dev build (see Scope) |

`data` is a single quote object (not a list) — matches
`milkful-app/lib/features/cart/models/quote.dart`'s `Quote.fromJson` exactly:
`basePrice`/`taxAmount`/`taxRate`/`deliveryFee`/`netPayable`/`monthlyEstimate`/`discountAmount`/
`appliedOfferId`.

## Scope — deliberately smaller than MA-122's full merged spec

MA-122 (the merged, reviewed spec) describes a much larger service: an Offers system
(percentage/flat/BOGO/first-order/subscription discounts with a precedence engine), Redis-cached
tax lookups, a new `CatalogUpdated` event Catalog Service would need to publish (requiring new
`hsnCode`/`gstRatePercent`/`active` fields on Catalog's own `Product` model — a real, if small,
reopening of an already-shipped service), and a full Aurora `offers`/`offer_redemptions`/
`product_tax` schema. See `specs/services/tasks/MA/MA-101/impl-plan/IMPLEMENTATION-PLAN.md` for
that full build.

This implementation is a **deliberately scoped-down subset**, built to unblock MA-23's mobile
screen — the service's only real caller today — without reopening Catalog Service or standing up
new infrastructure (Aurora, Redis) purely for a screen that never uses offers at all
(`ProductConfigScreen`'s `offerCode` is always `null` — no offer/coupon UX exists anywhere in the
mobile app yet). Specifically, this build:

- **Has no database at all.** No `offers`/`product_tax` tables, no migrations. `POST
  /pricing/quote` calls Catalog Service's already-live `GET /products/{id}` (MA-116 FR-2)
  synchronously for each line item's price instead of maintaining a `CatalogUpdated`-sourced local
  read model — simpler and equally correct for a local-dev service where synchronous latency isn't
  a real concern, at the cost of the spec's own <300ms p99 NFR target (not measured or targeted
  here).
- **Applies one flat, configurable tax rate** (`PRICING_DEFAULT_TAX_RATE_PERCENT`, default `5.0`)
  to every line item, not a real per-product HSN/GST rate — no `hsnCode`/`gstRatePercent` data
  exists anywhere in this platform (confirmed by reading `services/catalog/src/domain/models.py`
  directly), and sourcing it would mean the Catalog Service schema change + new `CatalogUpdated`
  publisher this build deliberately doesn't include.
- **Has no CGST/SGST-vs-IGST split.** The mobile client's `Quote` model only carries one
  `taxAmount`/`taxRate` pair — the split only changes how the *same* total tax is divided for GST
  invoice reporting (out of scope — see MA-38, Invoice Generation), not the total itself, so
  `deliveryState` is validated as present (per MA-122 FR-1's contract — a request without one is
  rejected the same way it would be against the full implementation) but not otherwise used in the
  tax calculation.
- **Applies one flat delivery fee** (`PRICING_DELIVERY_FEE`, default `₹20`) per quote, not a real
  fee schedule.
- **Has no Offers system.** `GET /offers` and `POST /offers/validate` aren't implemented; an
  `offerCode` in the request is accepted (for contract parity) but never applied —
  `discountAmount`/`appliedOfferId` are always `null` in the response, which the mobile client
  already treats as optional.
- **Has no Redis cache.** A pure NFR/performance optimization, not needed for local-dev
  correctness.
- **No CDK/`infra/` stack** — same reasoning as `services/cart`'s own documented decision: not
  needed to prove out and locally run/test the service, and an untested CDK stack isn't worth the
  surface area. Flagged as follow-up work, not an oversight.

Every one of these is a real gap versus the merged spec, not silently smoothed over — the goal was
an honest, working `POST /pricing/quote` for the one caller that exists today, not a partial
implementation dressed up as the full one.

## Configuration

All in `config/env.py`, env-prefixed `PRICING_`, every field has a real default (no `.env.local`
required — unlike catalog/user/inventory, this service has no secrets to source from
`bootstrap.py`):

| Var | Default | Meaning |
|---|---|---|
| `PRICING_CATALOG_BASE_URL` | `http://localhost:8003` | Where `GET /products/{id}` is called. |
| `PRICING_CATALOG_TIMEOUT_SECONDS` | `5.0` | Per-attempt timeout for that call. |
| `PRICING_DEFAULT_TAX_RATE_PERCENT` | `5.0` | Flat placeholder rate — confirm a real value with the business/finance owner before this is ever more than a local-dev placeholder. |
| `PRICING_DELIVERY_FEE` | `20.0` | Flat placeholder delivery fee. |
| `PRICING_CORS_ALLOW_ALL` | unset | Local-dev only, matches catalog/inventory's own toggle. |

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate  # or .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
pytest                          # unit only — no DB/AWS needed at all
```

Running it (needs Catalog Service reachable at `PRICING_CATALOG_BASE_URL`):

```bash
cd pricing-offer && python src/main.py   # :8005
```

Or via `services/local-dev`'s `docker compose up -d` — see that directory's own README.

```bash
curl -X POST localhost:8005/pricing/quote \
  -H "Content-Type: application/json" \
  -d '{"items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}], "deliveryState": "Karnataka"}'
```
