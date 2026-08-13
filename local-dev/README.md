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

# 2. Call User Service's register endpoint (needs a Bearer token carrying a phone_number
#    claim — see "Known gaps" below for why accessToken doesn't currently work here)
curl -X POST localhost:8002/users/register -H "Authorization: Bearer <accessToken>" -d '{...}'

# 3. Log in again later
curl -X POST localhost:8001/v1/auth/login/otp/send -d '{"mobile": "+919876543210"}'
python peek_otp.py +919876543210        # now also shows template=login
curl -X POST localhost:8001/v1/auth/login/otp/verify \
  -d '{"mobile": "+919876543210", "otp": "<code>", "requestId": "<from send response>"}'
# -> accessToken, refreshToken (no isNewUser)

curl localhost:8002/users/me -H "Authorization: Bearer <accessToken>"   # same gap as step 2

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
| `_lambda_local_server.py` | Generic HTTP-to-Lambda-event shim (stdlib only). Each service's `run_local.py` supplies its own `{(method, path): handler}` table. |
| `peek_otp.py` | Local-only OTP visibility, since there's no real SMS provider to read the code from. |
| `config/env.py`'s `env_file` | Each service's `Settings` now also reads a local `.env.local` (via `pydantic-settings`' `env_file`) if one exists — silently ignored when absent, so deployed environments (which never have this file) are unaffected. |
| `aws_endpoint_url` | New optional setting on all three services; threaded into every `boto3.client(...)`/`boto3.resource(...)` call. `None` (the default, and the only value ever set in a real deployment) means "use real AWS"; `bootstrap.py` sets it to `http://localhost:5000` in the generated `.env.local` files. |

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
- **`/users/register` and `/users/me` need a `phone_number` claim that `accessToken` doesn't
  carry.** identity-auth's `/v1/auth/otp/verify` and `/v1/auth/login/otp/verify` only ever return
  `accessToken`/`refreshToken` (never `idToken` — an existing, deliberate spec decision: "idToken
  is available on the TokenBundle but intentionally not added to the response since the spec
  doesn't ask for it"). Cognito access tokens never carry custom/profile attributes regardless of
  pool config, so following steps 2 and 5 above exactly as written gets a 400
  `Missing or invalid JWT claims` from User Service. This wasn't caught earlier because this half
  of the flow was never exercised end-to-end before (see the Docker-availability gap above) — it's
  a real gap in the identity-auth ↔ user-service contract, not just a docs error, and needs a
  product/spec decision (return `idToken` too? have User Service accept either token type and
  fall back to a GetUser call for `phone_number`?) rather than being silently decided here.
- **moto_server is a Flask dev server** — under rapid concurrent local testing (e.g. hammering
  it with several curl calls back-to-back) it can be slow enough to trip a short client timeout.
  Not a bug in this tooling; give it a few seconds between rapid-fire manual requests, or raise
  `curl --max-time`.
- **Inventory's own local run isn't yet exercised end-to-end here** (no serviceability zones
  seeded) — `apply_migrations.py` creates the schema, but nothing seeds `serviceability_zones`,
  so User Service's registration call to Inventory will get a 404/empty result until some rows
  exist. Manual `INSERT` or a seed script is a natural next step, not done here.
