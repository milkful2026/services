# Local development environment

Runs registration ([MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1)),
login ([MA-21](https://milkfuldairyindia.atlassian.net/browse/MA-21)), and product catalog
browsing ([MA-22](https://milkfuldairyindia.atlassian.net/browse/MA-22)) end-to-end on your own
machine, with no real AWS account and no deploy. AWS is stood in for by
[`moto_server`](https://github.com/getmoto/moto) (Cognito, DynamoDB, SQS, EventBridge — one
process, one port); Postgres and Redis are the real thing, just local containers.

This is dev tooling only — nothing here is used in any deployed environment. Each service's own
Lambda handlers are unmodified; a thin local HTTP shim (`_lambda_local_server.py`) just invokes
them the way API Gateway would.

## Prerequisites

- Docker Desktop running (`docker ps` should succeed)

That's the only prerequisite for the all-Docker path below. The native (non-Docker) path further
down additionally needs each service's own venv set up per its README (`identity-auth/README.md`,
`user/README.md`, `inventory/README.md`, `catalog/README.md`) — `python -m venv .venv && pip
install -r requirements-dev.txt` in each — and, from this directory, `pip install -r
requirements.txt` (boto3, psycopg2-binary — used by `bootstrap.py`/`apply_migrations.py`/
`peek_otp.py`, not by the services themselves).

## Option A: everything in Docker (recommended)

```bash
cd services/local-dev
docker compose up -d
```

One command brings up the whole stack: `moto_server` (:5000), Postgres (:5432), Redis (:6379),
a one-shot `bootstrap` container that waits for those to be healthy and then runs
`bootstrap.py` + `apply_migrations.py` + all three `seed_*.py` scripts, and finally the four app
services — identity-auth (:8001), user (:8002), inventory (:8000), catalog (:8003) — each built
from its own `Dockerfile` (`identity-auth/Dockerfile`, `user/Dockerfile`, `catalog/Dockerfile`;
`inventory/Dockerfile` already existed, it's also inventory's real production image).

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

**Not containerized**: `user/run_local_outbox_publisher.py` and `peek_otp.py` are one-off
dev-only scripts, not long-running services — run those two natively (own venv) alongside the
Docker stack, same as before:

```bash
cd user && python run_local_outbox_publisher.py       # polls outbox every 5s (stands in for the
                                                       # real rate(1 minute) EventBridge Schedule)
```

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

## How this fits together

| Piece | What it does |
|---|---|
| `docker-compose.yml` | `moto_server` (all of Cognito/DynamoDB/SQS/EventBridge on one port), `postgres` (three databases, `milkful_user` + `milkful_inventory` + `milkful_catalog`, via `init-databases.sql`), `redis`, a one-shot `bootstrap` container, and (Option A) the four app services themselves — each built from its own `Dockerfile`. |
| `Dockerfile.bootstrap` | Builds the one-shot `bootstrap` service's image (repo-root build context) — installs `local-dev/requirements.txt`, copies the whole repo in (needed for `apply_migrations.py`'s and the seed scripts' access to each service's `migrations/*.sql`), then runs `bootstrap.py && apply_migrations.py && seed_inventory_zones.py && seed_user_zone_slots.py && seed_catalog_products.py` and exits — the four app services' `depends_on: condition: service_completed_successfully` waits on that exit. |
| `identity-auth/Dockerfile`, `user/Dockerfile` | Repo-root build context (so they can also copy `local-dev/_env_file.py` + `local-dev/_lambda_local_server.py`, which `run_local.py` needs) — otherwise just `pip install -r requirements-dev.txt` (needed for the shim's `PyJWT` dependency) then `python run_local.py`. |
| `catalog/Dockerfile` | Own-directory build context, same shape as the pre-existing `inventory/Dockerfile` — no `local-dev/` dependency, since `catalog/src/main.py`/`inventory/src/main.py` are real, self-contained FastAPI entrypoints. |
| `bootstrap.py` | Creates the Cognito pool/client, `otp_requests` DynamoDB table, `zone-updated`/`otp-requested-debug`/`stock-changed` SQS queues (each with a DLQ where applicable), and the EventBridge rules routing to them — the direct-boto3 equivalent of what `cdk deploy` provisions for real. Writes each service's `.env.local`, plus dummy `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (moto doesn't check these, but boto3's client construction raises `NoCredentialsError` without *something* present — true in a fresh container with no `~/.aws/credentials` even though it wasn't previously an issue on a host that already had these exported globally). `LOCAL_DEV_AWS_ENDPOINT_URL`/`LOCAL_DEV_DB_HOST`/`LOCAL_DEV_REDIS_HOST`/`LOCAL_DEV_INVENTORY_HTTP_URL` override the `localhost`-based defaults (used by the `bootstrap` compose service, pointing at `moto`/`postgres`/`redis`/`inventory` container hostnames instead); `LOCAL_DEV_ENV_OUTPUT_ROOT` overrides where `.env.local` gets written (a shared docker volume per service, for Option A, instead of the host path). |
| `apply_migrations.py` | Runs each service's real `migrations/*.sql` against its local Postgres database, tracked in a `schema_migrations` table so re-runs only apply new files. |
| `seed_inventory_zones.py` | Inserts one serviceability zone (pincode prefix `5600`) directly via SQL — there's no admin/write API for zones (MA-95 is read-only), so this is the only local option. Upserts, safe to re-run. |
| `seed_user_zone_slots.py` | Inserts matching delivery slots into User Service's own `zone_slots` table (same `blr-central` zone id as above) — a separate table from Inventory's, not synced to it in any environment (see `user/README.md`'s flagged decision #1). Upserts, safe to re-run. |
| `seed_catalog_products.py` | Inserts 5 categories + 8 products directly via SQL — same "no admin/write API yet" reasoning as the zone/slot seeds above (MA-42's territory). Upserts, safe to re-run. |
| `_db.py` | Shared local-Postgres connection settings/helper used by every `seed_*.py` script, plus a friendly-error wrapper for "Postgres isn't up yet" / "migrations haven't been applied yet". |
| `_zone_seed_data.py` | Shared zone/slot fixture data used by `seed_inventory_zones.py` and `seed_user_zone_slots.py`, so the two independently-seeded tables can't drift out of sync with each other. |
| `_catalog_seed_data.py` | Shared category/product fixture data used by `seed_catalog_products.py` — category ids/icon names match the Flutter catalog screen's own icon-mapping switch exactly. |
| `_lambda_local_server.py` | Generic HTTP-to-Lambda-event shim (stdlib only). Each service's `run_local.py` supplies its own `{(method, path): handler}` table. Binds `0.0.0.0`, not `127.0.0.1` — a loopback-only bind works for a native/host run (the process *is* the machine) but is unreachable from outside a container's own network namespace, which is what Option A's published ports need. |
| `peek_otp.py` | Local-only OTP visibility, since there's no real SMS provider to read the code from. |
| `_env_file.py` | Loads `.env.local` into the real process environment (`os.environ`, via `setdefault` so real env vars always win) before any handler module is imported — used by each `run_local.py`/`run_local_outbox_publisher.py`; inventory's and catalog's `main.py` each carry a small inline duplicate since `local-dev/` isn't shipped in their container images. All four accept `ENV_LOCAL_PATH` to override where `.env.local` is read from (defaulting to the service's own directory) — set by the app services in Option A to point at the shared docker volume `bootstrap` wrote into, instead of a host path. |
| `AWS_ENDPOINT_URL` | The standard, unprefixed env var botocore already reads natively — no application code needed. `bootstrap.py` writes it into each generated `.env.local`, pointing at `http://localhost:5000` (native) or `http://moto:5000` (Option A, via `LOCAL_DEV_AWS_ENDPOINT_URL`); unset in every real deployment, so behavior there is unaffected. |

## Known gaps

- ~~No Docker daemon was available in the sandbox this was built in~~ — since resolved: the full
  `docker compose up -d` path (Option A above, including the `bootstrap` one-shot container and
  all four app services) has been verified end-to-end on a machine with Docker Desktop running —
  cold `down` then `up` re-provisions everything and all four services respond correctly with no
  manual steps.
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
- **Catalog's `StockChanged` consumer has no real producer yet** — Inventory's reserve/commit/
  release (MA-95/MA-118) is spec'd but not implemented, so nothing publishes this event in normal
  operation. The consumer itself is implemented and tested (unit tests with a mocked SQS queue,
  plus a real manual publish against the local `moto_server` queue — see "Exercising the catalog"
  above) against the payload contract both specs agreed on, ahead of the producer landing.
