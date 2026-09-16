# Payment Service

Razorpay wallet-recharge slice: server-side gateway orders, an
authoritative webhook, idempotent recharge lifecycle, and a
reconciliation sweep for anything the webhook misses.

Implements `specs/services/tasks/MA/MA-99/MA-126.md`, scoped to
`purpose = WALLET_RECHARGE`. The generic `purpose = ORDER` (cart
checkout) path is designed-for (the enum carries it) but not built here.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/payments` | Cognito JWT + `Idempotency-Key` | Create a Razorpay order for a recharge |
| POST | `/payments/{id}/confirm` | Cognito JWT (owner) | Record the client's signed callback → `CONFIRMING` (never credits) |
| POST | `/payments/webhook` | Razorpay signature (public, no JWT) | **Authoritative** → `CONFIRMED` / `FAILED` |
| GET | `/payments/{id}` | Cognito JWT (owner) | Read current state |

## Lifecycle

```
CREATED --(orders.create ok)--> CREATED(+order_id)
   |                                  | client pays in Razorpay
   |                                  v
   |                          POST /confirm (valid sig) --> CONFIRMING
   |                                  |
   +---- webhook payment.captured / reconcile ----> CONFIRMED --> outbox: PaymentConfirmed
   +---- webhook payment.failed / reconcile hard-cap -> FAILED --> outbox: PaymentFailed
```

`CONFIRMING` is optional — the webhook can beat the client's own
`confirm` call. A `FAILED` row with `failure_code = TIMEOUT` is
**provisional**: a later `payment.captured` webhook recovers it to
`CONFIRMED` (`LATE_CAPTURE_RECOVERED`). A `FAILED` row from a real
Razorpay `payment.failed` is terminal.

## Idempotency (three layers)

1. Client `Idempotency-Key` — `UNIQUE(user_id, idempotency_key)`. A dup
   key with a matching amount/purpose/currency replays the stored
   create-response (or resumes `orders.create` if a prior attempt never
   got an order id); a dup key with a **different** amount/purpose/
   currency is `409 IDEMPOTENCY_KEY_REUSED`.
2. Gateway order — `razorpay_order_id UNIQUE`.
3. Webhook/reconcile — every state-changing branch is idempotent
   (`WEBHOOK_DUP` on a repeat).

## Events

- **Consumes:** none (reads Wallet Service's `GET /wallet/internal/limits`
  over HTTP, not events).
- **Publishes** (transactional outbox → EventBridge `milkful-events`):
  `PaymentConfirmed`, `PaymentFailed`. A dedicated rule
  (`payment-confirmed-wallet-recharge`, `detail.purpose=WALLET_RECHARGE`)
  routes `PaymentConfirmed` to Wallet Service's `wallet-events-q`.

Schemas: `services/shared/events/PaymentConfirmed.schema.json`,
`PaymentFailed.schema.json` — the authoritative cross-service contract.

## Reconciliation sweep

Runs every `PAYMENT_RECONCILE_INTERVAL_SECONDS` (default 120s) against
rows `CONFIRMING`/`CREATED-with-an-order` older than
`PAYMENT_RECONCILE_STALE_SECONDS` (default 180s): polls Razorpay's own
order-payments list.

- Captured → confirms (real).
- All attempts failed → fails (real, terminal).
- Still payable, age ≥ `PAYMENT_RECONCILE_CONFIRMING_ALERT_SECONDS`
  (default 30 min) → stays `CONFIRMING`, emits `payment.confirming_over_30m`.
- Still payable, age ≥ `PAYMENT_RECONCILE_HARD_CAP_SECONDS` (default 6h)
  → fails with `failure_code = TIMEOUT` (**provisional** — see Lifecycle).

## Data (Aurora `payments`)

`payments(id, user_id, purpose, amount_paise BIGINT, status, method,
razorpay_order_id UNIQUE, idempotency_key, UNIQUE(user_id,
idempotency_key), failure_code, failure_reason, correlation_id, …)`,
`payment_events` (immutable audit of every state-affecting input),
`outbox`. Money is integer paise everywhere.

## Metrics

No CloudWatch client in this codebase yet — every metric is a
structured log line with a stable `metric` field (a CloudWatch Logs
metric filter target): `recharge.created`, `recharge.confirming`,
`recharge.confirmed`, `recharge.failed{code}`, `webhook.received{event}`,
`webhook.signature_invalid`, `recharge.reconciled{outcome}`,
`payment.confirming_over_30m`, `outbox.publish_lag_seconds`.

## Local development

Listens on `:8007`. FastAPI + a background reconciliation-sweep thread,
same single-deployable shape as `catalog`/`inventory`/`wallet`.

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.local.example .env.local   # fill in rzp_test_* credentials
python src/main.py                 # HTTP + reconcile loop
python src/handlers/outbox_publisher.py   # drains the outbox to EventBridge
```

## Tests

```
pytest
```

SQLite in-memory stands in for Aurora; a `FakeGateway` stands in for
Razorpay (no real credentials needed for unit/integration tests — see
`tests/conftest.py`). A real Razorpay-test-mode pass (`rzp_test_*`
UPI/card flows) is the manual acceptance step once credentials are
provisioned.

## Not yet done in this scaffold

- `infra/` CDK stack (Fargate service, Aurora ref, Secrets Manager,
  the `payment-confirmed-wallet-recharge` EventBridge rule, API Gateway
  routes incl. the public webhook + WAF rate-limit).
- `services/local-dev` wiring (port 8007, DB `milkful_payment`, the
  EventBridge rule against moto) — plan Step 5.
- The real Razorpay-test-mode manual pass (needs `rzp_test_*` credentials).
