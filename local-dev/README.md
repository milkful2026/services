# Local development environment

Runs registration ([MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1)),
login ([MA-21](https://milkfuldairyindia.atlassian.net/browse/MA-21)), product catalog
browsing ([MA-22](https://milkfuldairyindia.atlassian.net/browse/MA-22)), Admin RBAC
([MA-47](https://milkfuldairyindia.atlassian.net/browse/MA-47)), and the Subscription Module
([MA-25](https://milkfuldairyindia.atlassian.net/browse/MA-25)) end-to-end on your own machine,
with no real AWS account and no deploy. AWS is stood in for by
[`moto_server`](https://github.com/getmoto/moto) (Cognito, DynamoDB, SQS, EventBridge — one
process, one port); Postgres and Redis are the real thing, just local containers.

This is dev tooling only — nothing here is used in any deployed environment. Each service's own
Lambda handlers are unmodified; a thin local HTTP shim (`_lambda_local_server.py`) just invokes
them the way API Gateway would.

## Prerequisites

- Docker Desktop running (`docker ps` should succeed)

That's the only prerequisite for the all-Docker path below. The native (non-Docker) path further
down additionally needs each service's own venv set up per its README (`identity-auth/`, `user/`,
`inventory/`, `catalog/`, `cart/`, `pricing-offer/`, `wallet/`, `payment/`, `subscription/`,
`order/`) — `python -m venv .venv && pip install -r requirements-dev.txt` in each — and, from
this directory, `pip install -r requirements.txt` (boto3, psycopg2-binary, requests — used by
`bootstrap.py`/`apply_migrations.py`/`peek_otp.py`/`run_daily_local.py`, not by the services
themselves).

## Option A: everything in Docker (recommended)

```bash
cd services/local-dev
docker compose up -d
```

One command brings up the whole stack: `moto_server` (:5000), Postgres (:5432), Redis (:6379),
a one-shot `bootstrap` container that waits for those to be healthy and then runs
`bootstrap.py` + `apply_migrations.py` + all three `seed_*.py` scripts, and every app service —
identity-auth (:8001), user (:8002), inventory (:8000), catalog (:8003), cart (:8004),
pricing-offer (:8005), wallet (:8006), payment (:8007), subscription (:8008), order (:8009) —
each built from its own `Dockerfile`, plus one outbox-drain sidecar per service that owns an
outbox table (`cart-outbox`, `wallet-outbox`, `payment-outbox`, `subscription-outbox`,
`order-outbox` — same image as the service, different `command:`). Until MA-25, only the first
four of these had a compose entry at all (see "Known gaps" below for why cart/wallet/payment/
pricing-offer's own entries, closed by MA-25's local-dev commit, were a real pre-existing gap and
not something new to this feature).

`bootstrap`'s output (`.env.local` per service, with `moto`/`postgres`/`redis`/`inventory`
container hostnames instead of `localhost`) is written into a shared docker volume per service,
not a host bind-mount — see `bootstrap.py`'s `LOCAL_DEV_ENV_OUTPUT_ROOT` and each app's
`ENV_LOCAL_PATH` env var. This sidesteps a real race: a host bind-mount of a file that doesn't
exist yet turns into an empty directory the moment the app container starts, before `bootstrap`
ever gets a chance to write the real file into it.

Same caveat as the native path: `moto_server` is in-memory, so `docker compose down` (not just
`stop`) — or a moto container restart — wipes Cognito/DynamoDB/SQS state. The next `docker
compose up -d` re-runs `bootstrap` from scratch automatically; you don't need to do anything by
hand. `docker compose down -v` additionally drops the Postgres data volume.

**Not containerized**: `user/run_local_outbox_publisher.py`, `wallet/src/handlers/
invariant_check_handler.py`, and `peek_otp.py` are one-off dev-only scripts, not long-running
services — run those natively (own venv) alongside the Docker stack, same as before:

```bash
cd user && python run_local_outbox_publisher.py       # polls outbox every 5s (stands in for the
                                                       # real rate(1 minute) EventBridge Schedule)
cd wallet && python src/handlers/invariant_check_handler.py   # nightly balance-invariant sweep;
                                                       # run once by hand rather than waiting a
                                                       # full day — not a long-running loop like
                                                       # the outbox drains above, so it isn't one
                                                       # of the containerized services either.
```

Once Subscription/Order Service are both up (`docker compose up -d` already starts them),
`python run_daily_local.py` fires Subscription Service's Daily Run on demand — see "Exercising
subscriptions and orders" below.

## Option B: native (no service containers)

Everything below this point (bootstrap/migrations/seeds, exercising the API, the architecture
table) applies to both options identically — Option A just runs the same scripts inside
containers instead of your own shell. Use this path if you want to edit a service's code and see
it live without rebuilding an image.

```bash
cd services/local-dev
docker compose up -d moto postgres redis   # just the infra, not the app services
python bootstrap.py               # creates Cognito pool/client, DynamoDB table, SQS queues —
                                   # writes identity-auth/.env.local, user/.env.local,
                                   # inventory/.env.local (gitignored, regenerated every run)
python apply_migrations.py        # applies each service's real migrations/*.sql to Postgres
python seed_inventory_zones.py    # seeds one zone (pincode 560001) — no admin API exists for
                                   # this, so without it every registration call gets rejected
                                   # as not-serviceable
python seed_user_zone_slots.py    # seeds matching delivery slots in User Service's own
                                   # zone_slots table — without it, GET /delivery/slots
                                   # returns empty and the Flutter app's slot screen has
                                   # nothing to select
python seed_catalog_products.py   # seeds 5 categories + 8 products — no admin/write API
                                   # exists for the catalog either, so without it the
                                   # Flutter app's Home/catalog screen has nothing to
                                   # browse
```

`bootstrap.py` and `apply_migrations.py` are safe to re-run. `docker compose down -v` clears
everything (moto_server's state is in-memory anyway — a container restart alone already wipes
it, so re-run `bootstrap.py` after any restart).

Each in its own terminal, service's own venv activated:

```bash
cd identity-auth && python run_local.py              # :8001 — otp/social/refresh/login/logout
cd user && python run_local.py                       # :8002 — register/delivery-slots/me
cd user && python run_local_outbox_publisher.py       # polls outbox every 5s (stands in for the
                                                       # real rate(1 minute) EventBridge Schedule)
cd inventory && python src/main.py                    # :8000 — this one's a real FastAPI app
                                                       # already; no shim needed
cd catalog && python src/main.py                      # :8003 — products/categories/search;
                                                       # also a real FastAPI app, same as
                                                       # inventory, no shim needed
cd pricing-offer && PRICING_CORS_ALLOW_ALL=true python src/main.py   # :8005 — pricing/quote; no
                                                       # bootstrap step needed — no DB/AWS at all,
                                                       # just calls catalog's GET /products/{id}.
                                                       # CORS must be set explicitly here (unlike
                                                       # catalog/inventory above, nothing writes it
                                                       # into a .env.local for this one) or a
                                                       # browser-based (Flutter web) caller gets a
                                                       # CORS preflight block that surfaces as a
                                                       # generic connection error, not anything
                                                       # naming CORS.
cd cart && python run_local.py                        # :8004 — cart CRUD (MA-96). Same Lambda
                                                       # shim as identity-auth/user above.
cd cart && python run_local_outbox_publisher.py       # polls the cart table's OUTBOX# rows every
                                                       # 5s, same pattern as user's own outbox
                                                       # publisher above (Postgres there, a
                                                       # DynamoDB Scan here).
cd wallet && python src/main.py                       # :8006 — balance/transactions/limits +
                                                       # the wallet-events-q consumer (MA-24
                                                       # MA-127). Real FastAPI app, no shim.
cd wallet && python src/handlers/outbox_publisher.py  # drains WalletCreated/WalletCredited to
                                                       # EventBridge, same 5s-poll shape as cart's.
cd wallet && python src/handlers/invariant_check_handler.py   # nightly balance-invariant sweep;
                                                       # run once by hand locally rather than
                                                       # waiting a full day.
cd payment && python src/main.py                      # :8007 — Razorpay recharge slice (MA-24
                                                       # MA-126) + the in-process reconcile-sweep
                                                       # thread. Needs payment/.env.local with
                                                       # real rzp_test_* credentials (copy
                                                       # payment/.env.local.example) for anything
                                                       # beyond startup — bootstrap.py provisions
                                                       # everything except those.
cd payment && python src/handlers/outbox_publisher.py # drains PaymentConfirmed/PaymentFailed to
                                                       # EventBridge, same 5s-poll shape as cart's.
cd subscription && python src/main.py                 # :8008 — subscriptions CRUD/lifecycle +
                                                       # POST /internal/run-daily (MA-25 MA-131).
                                                       # Real FastAPI app, no shim, no consumer
                                                       # thread — nothing to consume.
cd subscription && python src/handlers/outbox_publisher.py   # drains SubscriptionOrderDue to
                                                       # EventBridge, same 5s-poll shape as cart's.
cd order && python src/main.py                        # :8009 — orders/me + the order-events-q
                                                       # consumer (MA-25 MA-132). Real FastAPI
                                                       # app, no shim.
cd order && python src/handlers/outbox_publisher.py   # drains OrderConfirmed/OrderPaymentFailed
                                                       # to EventBridge, same 5s-poll shape as
                                                       # cart's.
cd local-dev && python run_daily_local.py             # fires POST :8008/internal/run-daily —
                                                       # the documented Scheduler-emulation gap
                                                       # (MA-131 §6/§11); run once per simulated
                                                       # "day" once you have an ACTIVE subscription
                                                       # due, then let the outbox drains above and
                                                       # order's own consumer carry it the rest of
                                                       # the way.
```

## Exercising registration + login

No real SMS provider exists locally — `peek_otp.py` reads the plaintext OTP off a debug SQS
queue `bootstrap.py` wires up for exactly this (subscribed to the same `identity.otp.requested`
event the real SMS integration would consume; nothing like it exists in production).

```bash
# 1. Register
curl -X POST localhost:8001/v1/auth/otp/send -d '{"mobile": "+919876543210"}'
python peek_otp.py +919876543210        # prints: mobile=... otp=... template=registration
curl -X POST localhost:8001/v1/auth/otp/verify \
  -d '{"mobile": "+919876543210", "otp": "<code>", "requestId": "<from send response>"}'
# -> accessToken, refreshToken, isNewUser: true

# 2. Call User Service's register endpoint
curl -X POST localhost:8002/users/register -H "Authorization: Bearer <accessToken>" -d '{...}'

# 3. Log in again later
curl -X POST localhost:8001/v1/auth/login/otp/send -d '{"mobile": "+919876543210"}'
python peek_otp.py +919876543210        # now also shows template=login
curl -X POST localhost:8001/v1/auth/login/otp/verify \
  -d '{"mobile": "+919876543210", "otp": "<code>", "requestId": "<from send response>"}'
# -> accessToken, refreshToken (no isNewUser)

curl localhost:8002/users/me -H "Authorization: Bearer <accessToken>"

# 4. Log out
curl -X POST localhost:8001/v1/auth/logout -H "Authorization: Bearer <accessToken>" \
  -d '{"refreshToken": "<refreshToken>"}'
```

Verified end-to-end against a real `moto_server` process while building this: registration
send/verify, login send/verify, and logout (including the documented `revoke_token` moto
fidelity gap — see `identity-auth/README.md` — handled gracefully, still returns 204).

## Exercising the catalog

No auth needed — these are all read endpoints. `filters` is repeated per facet
(`category:{id}`, `price:{min}-{max}`, `veg:true`, `organic:true`); OpenSearch isn't stood up
locally (or anywhere yet — see Known gaps), so `GET /search` runs the same query directly
against this service's own Postgres table instead.

```bash
curl localhost:8003/categories
curl "localhost:8003/products?categoryId=milk"
curl localhost:8003/products/cow-milk
curl "localhost:8003/search?q=cow"
curl "localhost:8003/search?filters=category:milk&filters=veg:true&sort=price_asc"
```

`StockChanged` (published by Inventory once MA-95's reserve/commit/release lands — spec'd, not
yet implemented) is consumed live by a background thread in the same process. To exercise it
manually before that exists, publish directly to the local queue:

```bash
python -c "
import json, boto3
sqs = boto3.client('sqs', region_name='us-east-1', endpoint_url='http://localhost:5000',
                    aws_access_key_id='local', aws_secret_access_key='local')
queue_url = sqs.get_queue_url(QueueName='stock-changed')['QueueUrl']
sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({
    'correlationId': 'manual-test',
    'payload': {'eventId': 'evt-1', 'productId': 'cow-milk', 'availableQuantity': 0,
                'stockState': 'OUT_OF_STOCK', 'availableFrom': None,
                'occurredAt': '2026-01-01T00:00:00Z'},
}))
"
curl localhost:8003/products/cow-milk   # stockState should now read OUT_OF_STOCK
```

## Exercising pricing

No auth needed. Requires `catalog` (and its seeded products) to already be up — every quote calls
`GET /products/{id}` on it directly. `deliveryState` is required (rejected with a 400 if missing)
but not otherwise used — see `pricing-offer/README.md`'s own "Scope" section for the full list of
what this build deliberately doesn't implement (no Offers, no HSN/GST-driven tax rate, no Redis).

```bash
curl -X POST localhost:8005/pricing/quote -H "Content-Type: application/json" -d '{
  "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
  "deliveryState": "Karnataka"
}'
# -> basePrice/taxAmount/taxRate/deliveryFee/netPayable, monthlyEstimate: null

curl -X POST localhost:8005/pricing/quote -H "Content-Type: application/json" -d '{
  "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "DAILY"}],
  "deliveryState": "Karnataka"
}'
# -> same fields, plus a populated monthlyEstimate (net payable per delivery x ~30)

curl -X POST localhost:8005/pricing/quote -H "Content-Type: application/json" -d '{
  "items": [{"productId": "no-such-product", "quantity": 1, "frequency": "ONE_TIME"}],
  "deliveryState": "Karnataka"
}'
# -> 404, errorCode: PRODUCT_PRICING_UNKNOWN
```

## Exercising subscriptions and orders

Full MA-25 chain, verified end-to-end against this exact Docker stack. Requires a registered
user (see "Exercising registration + login" above) with an `<accessToken>` — registration now
auto-provisions a wallet for the new user via `UserRegistered` (MA-134, see "Known gaps" below
for the history), so no manual funding step is needed before recharging it:
`POST /wallet/me/recharge`'s real Razorpay path.

```bash
# 1. Create a DAILY subscription starting today. slotId isn't validated against real zone slots
#    yet (MA-133's mobile slot picker — see the MA-25 implementation plan Step 6 — is what will
#    resolve a real one; any non-empty string works here).
curl -X POST localhost:8008/subscriptions -H "Authorization: Bearer <accessToken>" \
  -H "Content-Type: application/json" -d '{
    "productId": "cow-milk", "quantity": 1, "schedule": {"type": "DAILY"},
    "startDate": "'"$(date +%Y-%m-%d)"'", "slotId": "morning-6-8",
    "idempotencyKey": "local-test-1"
  }'
# -> subscriptionId, status: ACTIVE, nextDeliveryDate (today if before the IST cutoff hour,
#    tomorrow otherwise — MA-131 §4's same-day-emission rule)

# 2. Fire the Daily Run (stands in for the real EventBridge Scheduler rule — see run_daily_local.py)
python run_daily_local.py
# -> dueSubscriptionIds: [<subscriptionId>] if it's due on the run's target date (always
#    "tomorrow" from the run's own perspective — see subscription_service.py's run_daily)

# 3. The subscription-outbox/order-outbox containers (already running under Option A) drain
#    SubscriptionOrderDue/OrderConfirmed within ~5s each; Order Service's own consumer thread
#    picks the message up from order-events-q, resolves deliveryState via User Service, quotes
#    via Pricing, debits via Wallet, all within the same request. Check the result:
curl localhost:8009/orders/me -H "Authorization: Bearer <accessToken>"
# -> one CONFIRMED order for the due delivery date, amountPaise matching the Pricing quote

curl localhost:8006/wallet/me -H "Authorization: Bearer <accessToken>"
# -> balancePaise decreased by exactly that amountPaise
```

Verified end-to-end against a real `moto_server` + Postgres while building the MA-25 local-dev
wiring: subscription create → Daily Run → SubscriptionOrderDue → Order Service materialize →
Pricing quote → Wallet debit → CONFIRMED order, wallet balance decremented by the exact quoted
amount.

## Exercising Admin RBAC (MA-47)

Two things moto and real AWS make impossible to run locally exactly as production code does are
stood in for by `_admin_local_dev.py`, loaded only by `identity-auth/run_local.py` — see that
file's module docstring for the full "why", confirmed empirically while wiring this up:

- **The real TOTP MFA challenge never happens against moto** — `AdminInitiateAuth` returns
  `AuthenticationResult` directly, no `ChallengeName`, no matter how the pool is configured
  (confirmed by direct probe, including via `SetUserPoolMfaConfig`). The real adapter correctly
  fails closed when that happens (a production safeguard against silently allowing single-factor
  login), so it can never complete a login against moto. Locally, the 2FA code is always
  **`123456`** (matching `portal-ui`'s own mock) instead of a real TOTP code — lockout after 5
  wrong attempts still works, since that logic never touches Cognito.
- **The admin authorizer's JWT signature check always fails against moto** — it fetches a *real*
  AWS JWKS URL, which can't resolve anything for a fake local pool ID. Locally, the JWT is decoded
  (claims only — `sub`, `role`, etc.) without verifying its signature, the same trust model
  `_lambda_local_server.py` already documents for the consumer JWT authorizer.

Bootstrapping the first Super-Admin is a manual, human-run step even locally (per
`scripts/bootstrap_super_admin.py`'s own design — there's no Super-Admin yet to call the real
invite API), but `--set-password` gives you an immediately-usable account rather than one stuck
in `Pending`/`FORCE_CHANGE_PASSWORD` with no activation path:

```bash
cd identity-auth
AWS_ENDPOINT_URL=http://localhost:5000 AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
python scripts/bootstrap_super_admin.py \
  --email superadmin@milkful.test \
  --name "Local Super Admin" \
  --admin-pool-id <IDENTITY_AUTH_ADMIN_COGNITO_USER_POOL_ID from .env.local> \
  --database-url postgresql+psycopg2://milkful:milkful@localhost:5432/milkful_identity_auth \
  --region us-east-1 \
  --execute \
  --set-password 'Passw0rd!'
```

Then, verified end-to-end against the real stack while building this:

```bash
# 1. Login (password step)
curl -X POST localhost:8001/v1/admin/auth/login \
  -d '{"email": "superadmin@milkful.test", "password": "Passw0rd!"}'
# -> challengeToken, expiresIn

# 2. 2FA verify — local code is always 123456
curl -X POST localhost:8001/v1/admin/auth/2fa/verify \
  -d '{"challengeToken": "<from step 1>", "code": "123456"}'
# -> accessToken, refreshToken, idToken, expiresIn

# 3. Admin CRUD — every /v1/admin/users* route needs the access token
curl localhost:8001/v1/admin/users -H "Authorization: Bearer <accessToken>"
curl -X POST localhost:8001/v1/admin/users -H "Authorization: Bearer <accessToken>" \
  -d '{"name": "Test Ops", "email": "ops@milkful.test", "role": "Ops"}'
curl -X POST localhost:8001/v1/admin/users/<id>/deactivate -H "Authorization: Bearer <accessToken>"
```

Without a token (or with a wrong 2FA code 5 times in 15 minutes), these correctly reject —
`403 forbidden` and `401 ADMIN_ACCOUNT_LOCKED` respectively, both confirmed live.

**portal-ui against this real backend, instead of its own MSW mocks:**

```bash
cd portal-ui
VITE_USE_MOCKS=false npm run dev
```

`vite.config.ts`'s dev-server proxy forwards this app's relative `/v1/...` calls to
`http://localhost:8001` — confirmed end-to-end (a bad-credentials login through the running Vite
dev server returned the real backend's actual `401 INCORRECT_CREDENTIALS`, not a mock). Omit the
env var (or set it to anything else) for the default, backend-independent MSW-mocked experience.

## How this fits together

| Piece | What it does |
|---|---|
| `docker-compose.yml` | `moto_server` (all of Cognito/DynamoDB/SQS/EventBridge on one port), `postgres` (eight databases, via `init-databases.sql`), `redis`, a one-shot `bootstrap` container, and (Option A) every app service plus one outbox-drain sidecar per service that owns an outbox table — each built from its own `Dockerfile`. |
| `Dockerfile.bootstrap` | Builds the one-shot `bootstrap` service's image (repo-root build context) — installs `local-dev/requirements.txt`, copies the whole repo in (needed for `apply_migrations.py`'s and the seed scripts' access to each service's `migrations/*.sql`), then runs `bootstrap.py && apply_migrations.py && seed_inventory_zones.py && seed_user_zone_slots.py && seed_catalog_products.py` and exits — every app service's `depends_on: condition: service_completed_successfully` waits on that exit. |
| `identity-auth/Dockerfile`, `user/Dockerfile`, `cart/Dockerfile` | Repo-root build context (so they can also copy `local-dev/_env_file.py` + `local-dev/_lambda_local_server.py`, which `run_local.py` needs) — otherwise just `pip install -r requirements-dev.txt` (needed for the shim's `PyJWT` dependency) then `python run_local.py`. `cart/Dockerfile` is new (MA-25's local-dev commit) — cart never had one before, since it only ever ran natively. |
| `catalog/Dockerfile` | Own-directory build context, same shape as the pre-existing `inventory/Dockerfile` — no `local-dev/` dependency, since `catalog/src/main.py`/`inventory/src/main.py` are real, self-contained FastAPI entrypoints. |
| `wallet/Dockerfile`, `payment/Dockerfile`, `subscription/Dockerfile`, `order/Dockerfile` | Repo-root build context (so they can also copy `shared/` — `shared.adapters.retry`, `shared.adapters.outbox_event_publisher`, `shared.handlers.auth`) — `pip install -r requirements.txt` then `python main.py`. The outbox-drain sidecar for each reuses the same image with `command: ["python", "-m", "handlers.outbox_publisher"]` (`-m`, not a bare script path — a plain `python handlers/outbox_publisher.py` puts `handlers/` on `sys.path[0]` instead of `/app`, breaking every sibling import; caught by actually running the container, not just `docker compose config`). |
| `pricing-offer/Dockerfile` | Own-directory build context, same shape as `catalog`/`inventory` — no DB, no AWS, no `.env.local` at all; `PRICING_CATALOG_BASE_URL`/`PRICING_CORS_ALLOW_ALL` are set directly in `docker-compose.yml`'s `environment:` block instead (see `pricing-offer/src/main.py`'s own docstring, which already anticipated this). |
| `bootstrap.py` | Creates the Cognito pool/client, `otp_requests` DynamoDB table, `zone-updated`/`otp-requested-debug`/`stock-changed` SQS queues (each with a DLQ where applicable), and the EventBridge rules routing to them — the direct-boto3 equivalent of what `cdk deploy` provisions for real. Writes each service's `.env.local`, plus dummy `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (moto doesn't check these, but boto3's client construction raises `NoCredentialsError` without *something* present — true in a fresh container with no `~/.aws/credentials` even though it wasn't previously an issue on a host that already had these exported globally). `LOCAL_DEV_AWS_ENDPOINT_URL`/`LOCAL_DEV_DB_HOST`/`LOCAL_DEV_REDIS_HOST`/`LOCAL_DEV_INVENTORY_HTTP_URL` override the `localhost`-based defaults (used by the `bootstrap` compose service, pointing at `moto`/`postgres`/`redis`/`inventory` container hostnames instead); `LOCAL_DEV_ENV_OUTPUT_ROOT` overrides where `.env.local` gets written (a shared docker volume per service, for Option A, instead of the host path). |
| `apply_migrations.py` | Runs each service's real `migrations/*.sql` against its local Postgres database, tracked in a `schema_migrations` table so re-runs only apply new files. |
| `seed_inventory_zones.py` | Inserts one serviceability zone (pincode prefix `5600`) directly via SQL — there's no admin/write API for zones (MA-95 is read-only), so this is the only local option. Upserts, safe to re-run. |
| `seed_user_zone_slots.py` | Inserts matching delivery slots into User Service's own `zone_slots` table (same `blr-central` zone id as above) — a separate table from Inventory's, not synced to it in any environment (see `user/README.md`'s flagged decision #1). Upserts, safe to re-run. |
| `seed_catalog_products.py` | Inserts 5 categories + 8 products directly via SQL — same "no admin/write API yet" reasoning as the zone/slot seeds above (MA-42's territory). Upserts, safe to re-run. |
| `_db.py` | Shared local-Postgres connection settings/helper used by every `seed_*.py` script, plus a friendly-error wrapper for "Postgres isn't up yet" / "migrations haven't been applied yet". |
| `_zone_seed_data.py` | Shared zone/slot fixture data used by `seed_inventory_zones.py` and `seed_user_zone_slots.py`, so the two independently-seeded tables can't drift out of sync with each other. |
| `_catalog_seed_data.py` | Shared category/product fixture data used by `seed_catalog_products.py` — category ids/icon names match the Flutter catalog screen's own icon-mapping switch exactly. |
| `_lambda_local_server.py` | Generic HTTP-to-Lambda-event shim (stdlib only). Each service's `run_local.py` supplies its own `{(method, path): handler}` table — a route's value is either a plain handler function, or a `(handler, authorizer)` tuple for routes behind a Lambda REQUEST authorizer (MA-129's admin routes; every other route in every service is unaffected). Binds `0.0.0.0`, not `127.0.0.1` — a loopback-only bind works for a native/host run (the process *is* the machine) but is unreachable from outside a container's own network namespace, which is what Option A's published ports need. |
| `_admin_local_dev.py` | MA-129-only. Two local-dev-only compensating adapters — `LocalDevAdminCognitoAdapter` (fakes the TOTP MFA challenge moto can't do) and `LocalDevUnsignedJwtVerifier` (skips the real JWKS signature check the admin authorizer can't do against a fake local pool) — loaded only by `identity-auth/run_local.py`, never touching `identity-auth/src/`. See its own module docstring for the full empirical reasoning. |
| `peek_otp.py` | Local-only OTP visibility, since there's no real SMS provider to read the code from. |
| `run_daily_local.py` | Fires Subscription Service's `POST /internal/run-daily` on demand — the documented Scheduler-emulation gap (MA-131 §6/§11); no local scheduler exists, so a developer triggers each simulated "day" by hand. |
| `_env_file.py` | Loads `.env.local` into the real process environment (`os.environ`, via `setdefault` so real env vars always win) before any handler module is imported — used by each `run_local.py`/`run_local_outbox_publisher.py`; inventory's and catalog's `main.py` each carry a small inline duplicate since `local-dev/` isn't shipped in their container images. All four accept `ENV_LOCAL_PATH` to override where `.env.local` is read from (defaulting to the service's own directory) — set by the app services in Option A to point at the shared docker volume `bootstrap` wrote into, instead of a host path. |
| `AWS_ENDPOINT_URL` | The standard, unprefixed env var botocore already reads natively — no application code needed. `bootstrap.py` writes it into each generated `.env.local`, pointing at `http://localhost:5000` (native) or `http://moto:5000` (Option A, via `LOCAL_DEV_AWS_ENDPOINT_URL`); unset in every real deployment, so behavior there is unaffected. |

## Known gaps

- ~~No Docker daemon was available in the sandbox this was built in~~ — since resolved: the full
  `docker compose up -d` path (Option A above, including the `bootstrap` one-shot container and
  every app service) has been verified end-to-end on a machine with Docker Desktop running — cold
  `down` then `up` re-provisions everything and every service responds correctly with no manual
  steps.
- ~~`WalletService.create_wallet` reads `user_registered["userId"]`, but the real `UserRegistered`
  event User Service emits never has that key~~ — fixed (MA-134). Two real bugs, found by actually
  registering a user against this Docker stack and watching `GET /wallet/me` stay stuck at
  `status: CREATING` / `balancePaise: 0` forever (unit tests on both sides construct their own
  `detail` dicts directly, so neither mismatch was ever exercised end-to-end before): (1)
  `registration_service.py`'s own outbox payload had `cognitoSub`, not `userId` — renamed, since
  every service's own `current_user_id()` resolves identity from the Cognito sub, so wallet rows
  are keyed by it too; and (2) User Service publishes `UserRegistered` through its own, older
  `adapters/outbox_event_publisher.py` (predates `shared/`'s, and Cart still has an identical
  copy), which wraps the actual domain payload one level deeper than every event published via
  the shared publisher does — `wallet_events_consumer.py` now unwraps that shape generically for
  every detail_type it handles, not just `UserRegistered`, so the next event type published
  through a legacy-shaped publisher doesn't reintroduce the same `KeyError`. `POST /wallet/me/retry`
  was never affected by either bug — it calls `create_wallet({"userId": user_id})` directly with
  the right key and no envelope, using the JWT `sub` already resolved by the HTTP layer.
- **`moto[server]` must be a recent version (>=5.2.2) if you're running it standalone instead of
  via `docker compose`** (e.g. because Docker isn't available, same fallback used while building
  and testing MA-21's login flow this session). `moto[server]==5.0.21` has a real bug where
  `list_users` with *any* `Filter` — used by `find_verified_sub_by_phone` (identity-auth's login
  gate) and `cognito_attribute_adapter`'s sub-lookup (User Service) — always returns empty, even
  for users that demonstrably exist (confirmed via unfiltered `list_users`). This makes login
  fail with a spurious `USER_NOT_FOUND` for an account that was just registered. Upgrading to
  `moto[server]==5.2.2` fixed it outright — full register → login → logout verified working
  end-to-end afterward. **Not a risk for the documented `docker compose up` path**, which already
  pulls `motoserver/moto:latest`; this only bites a manual non-Docker fallback with a stale
  cached install.
- **JWT claims aren't verified, only decoded.** `_lambda_local_server.py` decodes whatever's in
  the `Authorization: Bearer` header via PyJWT's `verify_signature=False` mode, without checking
  its signature — real API Gateway's Cognito JWT authorizer verifies it first. Fine for a
  developer's own machine; this must never be treated as equivalent to the real authorizer.
- **moto doesn't honor `UsernameAttributes=["phone_number"]`.** Real Cognito, with this pool's
  actual config, sets `Username` to the literal value passed to `AdminCreateUser` — identity-auth's
  `cognito_adapter.py` and User Service's `cognito_attribute_adapter.get_mobile_by_sub` both
  depend on this (Username *is* the mobile number). moto instead always assigns a random UUID as
  Username (reusing it as `sub` too) regardless of `UsernameAttributes`. Practical effect locally:
  `GET /users/me`'s `mobile` field (and anywhere else mobile is resolved via Cognito) will show
  that UUID, not a real-looking phone number, when run against moto_server — cosmetic only, the
  actual resolution logic is correct and verified against real Cognito's documented behavior (see
  `user/tests/unit/adapters/test_cognito_attribute_adapter.py`, which stubs around this gap rather
  than relying on moto to reproduce it).
- **moto_server is a Flask dev server** — under rapid concurrent local testing (e.g. hammering
  it with several curl calls back-to-back) it can be slow enough to trip a short client timeout.
  Not a bug in this tooling; give it a few seconds between rapid-fire manual requests, or raise
  `curl --max-time`.
- **Inventory's own local run (and `seed_inventory_zones.py`/`seed_user_zone_slots.py`) hasn't
  been exercised against a live Postgres container** — same root cause as the Docker-availability
  gap above. Both seed scripts' SQL was written against the real, already-tested
  `inventory/migrations/0001_serviceability_zones.sql` and
  `user/migrations/0001_users_addresses_consents.sql` schemas, but neither was run against an
  actual Postgres instance in this sandbox.
- **Catalog's `GET /search` runs directly against Postgres (`ILIKE` + `WHERE` + `ORDER BY`), not
  real OpenSearch.** MA-117's own spec flags OpenSearch as the single biggest implementation risk
  in that whole spec set — no local-dev emulation precedent exists for it anywhere in this repo.
  This same-database implementation satisfies the documented API *contract* (same request params,
  same response shape) so both sides of the Catalog↔mobile contract agree on behavior; swapping the
  query engine underneath to real OpenSearch later doesn't change that contract. One concrete gap
  from this: `sort=newest` currently falls back to name-order, since there's no recency column in
  the Postgres schema today (never added — the Aurora-only implementation didn't need one for
  price sort, and this was noticed only when writing this deviation note).
- **Found and fixed while wiring this up: `identity-auth/migrations/0001_admin_user.sql`'s
  `ip_allowlist` column was declared `TEXT[]`, but `admin_user_repository.py`'s SQLAlchemy Core
  table declared it `JSON` — a genuine Postgres type mismatch (not a local-dev-only issue; this
  would have broken every admin create/update against real Aurora too) that broke every insert.
  The offline pytest suite (SQLite, which doesn't enforce this distinction) never caught it —
  only running against real Postgres here did. `0001` had already been merged by the time this was
  found, so the fix is a new `migrations/0002_fix_admin_ip_allowlist_type.sql` (drops and re-adds
  the column as `JSONB`, matching `user` service's own working `lines` column precedent), not an
  edit to `0001` — per `services/README.md` §3.6's migration-immutability rule. **If you already
  ran this stack before this fix landed**, your local `milkful_identity_auth` database has the
  broken column and `apply_migrations.py`'s per-filename tracking won't retroactively fix it —
  run `docker compose down -v` (or manually drop that one database) before bringing the stack back
  up, same as any other schema-breaking local change.
- **Catalog's `StockChanged` consumer has no real producer yet** — Inventory's reserve/commit/
  release (MA-95/MA-118) is spec'd but not implemented, so nothing publishes this event in normal
  operation. The consumer itself is implemented and tested (unit tests with a mocked SQS queue,
  plus a real manual publish against the local `moto_server` queue — see "Exercising the catalog"
  above) against the payload contract both specs agreed on, ahead of the producer landing.
