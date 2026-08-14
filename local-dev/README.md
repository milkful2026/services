# Local development environment

Runs registration ([MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1)) and
login ([MA-21](https://milkfuldairyindia.atlassian.net/browse/MA-21)) end-to-end on your own
machine, with no real AWS account and no deploy. AWS is stood in for by
[`moto_server`](https://github.com/getmoto/moto) (Cognito, DynamoDB, SQS, EventBridge — one
process, one port); Postgres and Redis are the real thing, just local containers.

This is dev tooling only — nothing here is used in any deployed environment. Each service's own
Lambda handlers are unmodified; a thin local HTTP shim (`_lambda_local_server.py`) just invokes
them the way API Gateway would.

## Prerequisites

- Docker Desktop running (`docker ps` should succeed)
- Each service's own venv set up per its README (`identity-auth/README.md`, `user/README.md`,
  `inventory/README.md`) — `python -m venv .venv && pip install -r requirements-dev.txt` in each
- From this directory: `pip install -r requirements.txt` (boto3, psycopg2-binary — used by
  `bootstrap.py`/`apply_migrations.py`/`peek_otp.py`, not by the services themselves)

## One-time-per-session setup

```bash
cd services/local-dev
docker compose up -d              # moto_server :5000, postgres :5432, redis :6379
python bootstrap.py               # creates Cognito pool/client, DynamoDB table, SQS queues —
                                   # writes identity-auth/.env.local, user/.env.local,
                                   # inventory/.env.local (gitignored, regenerated every run)
python apply_migrations.py        # applies each service's real migrations/*.sql to Postgres
python seed_inventory_zones.py    # seeds one zone (pincode 560001) — no admin API exists for
                                   # this, so without it every registration call gets rejected
                                   # as not-serviceable
```

`bootstrap.py` and `apply_migrations.py` are safe to re-run. `docker compose down -v` clears
everything (moto_server's state is in-memory anyway — a container restart alone already wipes
it, so re-run `bootstrap.py` after any restart).

## Running the services

Each in its own terminal, service's own venv activated:

```bash
cd identity-auth && python run_local.py              # :8001 — otp/social/refresh/login/logout
cd user && python run_local.py                       # :8002 — register/delivery-slots/me
cd user && python run_local_outbox_publisher.py       # polls outbox every 5s (stands in for the
                                                       # real rate(1 minute) EventBridge Schedule)
cd inventory && python src/main.py                    # :8000 — this one's a real FastAPI app
                                                       # already; no shim needed
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

## How this fits together

| Piece | What it does |
|---|---|
| `docker-compose.yml` | `moto_server` (all of Cognito/DynamoDB/SQS/EventBridge on one port), `postgres` (two databases, `milkful_user` + `milkful_inventory`, via `init-databases.sql`), `redis` |
| `bootstrap.py` | Creates the Cognito pool/client, `otp_requests` DynamoDB table, `zone-updated`(+DLQ) and `otp-requested-debug` SQS queues, and the EventBridge rules routing to them — the direct-boto3 equivalent of what `cdk deploy` provisions for real. Writes each service's `.env.local`. |
| `apply_migrations.py` | Runs each service's real `migrations/*.sql` against its local Postgres database, tracked in a `schema_migrations` table so re-runs only apply new files. |
| `seed_inventory_zones.py` | Inserts one serviceability zone (pincode prefix `5600`) directly via SQL — there's no admin/write API for zones (MA-95 is read-only), so this is the only local option. Upserts, safe to re-run. |
| `_lambda_local_server.py` | Generic HTTP-to-Lambda-event shim (stdlib only). Each service's `run_local.py` supplies its own `{(method, path): handler}` table. |
| `peek_otp.py` | Local-only OTP visibility, since there's no real SMS provider to read the code from. |
| `_env_file.py` | Loads `.env.local` into the real process environment (`os.environ`, via `setdefault` so real env vars always win) before any handler module is imported — used by each `run_local.py`/`run_local_outbox_publisher.py`; inventory's `main.py` carries a small inline duplicate since `local-dev/` isn't shipped in its container image. |
| `AWS_ENDPOINT_URL` | The standard, unprefixed env var botocore already reads natively — no application code needed. `bootstrap.py` writes it into each generated `.env.local`, pointing at `http://localhost:5000`; unset in every real deployment, so behavior there is unaffected. |

## Known gaps

- **No Docker daemon was available in the sandbox this was built in**, so the Postgres/Redis
  containers and `docker compose up` itself could not be run end-to-end there. `bootstrap.py`
  and the full identity-auth login flow (registration, login, logout) *were* verified directly
  against a real `moto_server` process (pure Python, no Docker needed to run it standalone) — so
  the AWS-facing half is proven; the Postgres-backed half (User Service register/get_me,
  Inventory serviceability) is implemented the same way but wasn't exercised against a live
  Postgres container. Worth a real run-through on a machine with Docker Desktop actually running.
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
- **Inventory's own local run (and `seed_inventory_zones.py`) hasn't been exercised against a
  live Postgres container** — same root cause as the Docker-availability gap above. The seed
  script's SQL was written against the real, already-tested `migrations/0001_serviceability_zones.sql`
  schema, but wasn't run against an actual Postgres instance in this sandbox.
