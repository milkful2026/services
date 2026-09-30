# Order Service

Turns a due subscription delivery into a real, paid Order: quotes the
current price via Pricing Service, debits the customer's wallet, and
records the outcome. Since MA-136 (MA-34) it also runs **cart checkout**:
one idempotent call that charges a cart's one-time lines as a single
order, starts its subscription lines, and clears them from the cart.

Implements `specs/services/tasks/MA/MA-25/MA-132.md` (MA-99/MA-25). New
service — no prior scaffold.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/orders/me` | Cognito JWT | Paged (keyset), newest-first, optionally filtered by `subscriptionId` (FR-3) |
| GET | `/orders/{id}` | Cognito JWT | One order's full detail (FR-3) |
| POST | `/orders/checkout` | Cognito JWT + `Idempotency-Key` | MA-136 cart checkout — see below |

Every order has a `source`: `SUBSCRIPTION` (materialized from
`SubscriptionOrderDue`, one product) or `CHECKOUT` (a cart's one-time
lines, listed in `items`; `subscriptionId` is null).

## Cart checkout flow (MA-136)

```
POST /orders/checkout {cartVersion, expectedPayNowPaise?} + Idempotency-Key
  -> (user, key) already has a checkout: COMPLETED/PAYMENT_FAILED -> replay,
     IN_PROGRESS -> resume at its step
  -> another IN_PROGRESS checkout for the user: 409 CHECKOUT_IN_PROGRESS
     naming it, or — untouched for 2 min (abandoned) — finish it first
  -> validate, nothing persisted: Cart internal
     read + version, line shape (slot, start date), User address state,
     Pricing quote of the one-time lines, price match, Wallet balance
     (pay-now + ₹500 minimum if any subscription line)
  -> one txn: checkouts(IN_PROGRESS, STARTED) + CREATED order + order_items
     (an order whenever there are one-time lines, even at ₹0)
  -> Wallet debit: DEBITED -> CONFIRMED (+OrderConfirmed); declined ->
     PAYMENT_FAILED, checkout ends, nothing else happens
  -> Subscription POST /internal/subscriptions per line
     (key checkout:{id}:{lineId}); a 4xx fails that line only
  -> Cart internal remove-items (the checked-out lines; on a version
     conflict, only those still exactly as checked out)
  -> COMPLETED, result stored for same-key replay
```

A dependency failing after the checkout started returns `503
CHECKOUT_INCOMPLETE`; retrying with the same key resumes, and never
charges twice (checkout key unique, one order per checkout, Wallet's own
order-id idempotency).

## Events

- **Consumes** (`order-events-q`, owned by this service): `SubscriptionOrderDue`
  from Subscription Service (MA-131).
- **Calls** (checkout only): Cart `GET /cart/internal/users/{id}` and
  `POST .../remove-items` (SigV4), Subscription `POST /internal/subscriptions`,
  Wallet `GET /wallet/internal/balance` — alongside the existing User /
  Pricing / Wallet-debit calls.
- **Publishes** (transactional outbox → EventBridge): `OrderConfirmed`,
  `OrderPaymentFailed`.

## Materialization flow

```
receive SubscriptionOrderDue
  -> (subscription_id, delivery_date) already has an Order row:
       CONFIRMED/PAYMENT_FAILED -> ack, done
       still CREATED (prior crash) -> resume at the debit call
  -> resolve deliveryState via User Service (SigV4-signed internal call)
  -> quote via Pricing Service (POST /pricing/quote, frequency=ONE_TIME)
  -> insert Order(CREATED) — no outbox row yet, outcome isn't known
  -> POST /wallet/internal/debit(userId, orderId, amountPaise, correlationId)
       DEBITED               -> Order.CONFIRMED + OrderConfirmed outbox row
       INSUFFICIENT_BALANCE/
       WALLET_NOT_ACTIVE     -> Order.PAYMENT_FAILED + OrderPaymentFailed outbox row
       transport failure     -> leave CREATED, do NOT ack (safe redelivery)
  -> ack SQS message only once CONFIRMED or PAYMENT_FAILED
```

`(subscription_id, delivery_date)` is UNIQUE at the DB level — the core
correctness guarantee, independent of the SQS-level message dedupe.

## Reconciliation sweep (MA-138 / MA-143)

A daemon thread (`handlers/sweep.py`, started by `main.py`) that finishes
or safely closes records left half-done by a crash or an outage:

- **Subscription orders stuck `CREATED`** (older than
  `ORDER_SUBSCRIPTION_ORDER_STALE_SECONDS`, e.g. the SQS message went to
  the DLQ): charged once through the same debit step `materialize`'s
  crash-resume uses (Wallet dedupes on the order id) while before the
  **charge deadline** (`ORDER_SUBSCRIPTION_CHARGE_DEADLINE_HOUR_IST`, 23:00
  IST the day before delivery — not the 20:00 cut-off, which has already
  passed when the Daily Run creates every subscription order). Past it the
  order is **closed without charge**: Wallet voids it first
  (`POST /wallet/internal/debits/{orderId}/void`, MA-142) → `NEEDS_ATTENTION(CUTOFF_PASSED)`,
  `charge_state = NOT_CHARGED`; if a lost debit already landed, the void
  says so and the order is confirmed instead (`charged_after_cutoff`
  alarm). An unreachable Wallet never closes an order.
- **Lease:** a record is worked only by the holder of
  `claimed_until`/`claim_owner` (DB clock). The SQS resume path takes the
  same lease; if the sweep holds it, the message is left unacked
  (`ORDER_BUSY`) and the redelivery finds the order terminal.
- **Budget:** each failed sweep attempt increments `sweep_attempts`; at
  `ORDER_SWEEP_MAX_ATTEMPTS` the order becomes
  `NEEDS_ATTENTION(SWEEP_EXHAUSTED)` with `charge_state = UNKNOWN` and is
  never retried for a charge.
- **Settle pass:** each run voids `NEEDS_ATTENTION` orders whose charge is
  `UNKNOWN` → `NOT_CHARGED`, or `CHARGED` + `sweep.settle.escalated_charged`
  (alarm: paid but not confirmed). `status` never changes here.

| Env var | Default |
|---------|---------|
| `ORDER_SWEEP_ENABLED` | `true` |
| `ORDER_SWEEP_INTERVAL_SECONDS` | `300` |
| `ORDER_SUBSCRIPTION_ORDER_STALE_SECONDS` | `900` |
| `ORDER_SUBSCRIPTION_CHARGE_DEADLINE_HOUR_IST` | `23` (must be after the 20:00 cut-off) |
| `ORDER_CHECKOUT_STALE_SECONDS` | `600` |
| `ORDER_SWEEP_MAX_ATTEMPTS` | `6` |
| `ORDER_SWEEP_LEASE_SECONDS` | `120` (minimum 60) |
| `ORDER_SWEEP_BATCH_SIZE` | `50` |

- **Abandoned checkouts** (`IN_PROGRESS`, untouched for
  `ORDER_CHECKOUT_STALE_SECONDS`, MA-144): resumed through the same steps
  a customer retry runs. Exceptions:
  - never charged and past the delivery cut-off → Wallet voids the order;
    voided (or a ₹0 order, no Wallet call) → checkout and order
    `CANCELLED` with no charge and the cart untouched; already debited →
    completed as normal. An unreachable Wallet never cancels. A debit
    refused `DEBIT_VOIDED` (another worker closed it) ends `CANCELLED`.
  - budget spent after payment, subscriptions still failing → completed,
    the unstarted lines left in the cart (`SUBSCRIPTION_UNAVAILABLE`) and
    their subscription keys carried (`carried_subscription_keys`), so the
    next checkout of that line replays a create that did land instead of
    creating a second subscription.
  - budget spent before we know about the charge, or at the cart clear
    → `NEEDS_ATTENTION` (the one-live-checkout lock is released).
  The customer path takes the same lease: a checkout the sweep holds
  returns `409 CHECKOUT_IN_PROGRESS` with `retryAfterSeconds`; a replay of
  a cancelled or escalated checkout returns `409 CHECKOUT_CANCELLED` /
  `409 CHECKOUT_NEEDS_ATTENTION`.

Metrics (log-based, `"metric"` field): `sweep.subscription_order.{found,resumed,confirmed,payment_failed,charged_after_cutoff,escalated,failed_attempt}`,
`sweep.settle.{settled_not_charged,escalated_charged}`,
`sweep.checkout.{found,resumed,completed,completed_partial,payment_failed,cancelled,charged_after_cutoff,escalated,failed_attempt}`
(`escalated` carries `reason`), `sweep.run_duration_ms`, `sweep.run_failed`.
`/healthz` returns 503 if the sweep thread dies.

## Data (Aurora `order`)

`orders(id, user_id, subscription_id, product_id, quantity,
amount_paise, delivery_date, status, failure_reason, created_at,
confirmed_at, source, checkout_id)` with `UNIQUE(subscription_id,
delivery_date)`, `UNIQUE(checkout_id)`, `CHECK(amount_paise >= 0)` and
the `orders_source_shape` CHECK; `order_items(order_id, line_no,
product_id, quantity)`; `checkouts(…)` with `UNIQUE(user_id,
idempotency_key)` and a partial unique index allowing one `IN_PROGRESS`
checkout per user (`migrations/0002_checkout.sql`); sweep lease/attempt
columns, `orders.charge_state` and `carried_subscription_keys`
(`migrations/0003_sweep.sql`); plus `outbox(…)`.

## Local development

Listens on `:8009`. FastAPI + a background SQS consumer thread, same
single-deployable shape as `wallet`/`payment`.

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python src/main.py                        # HTTP + order-events consumer
python src/handlers/outbox_publisher.py   # drains the outbox to EventBridge
```

## Tests

```
pytest
```

SQLite in-memory stands in for Aurora (documented fidelity gap, same as
every other service here); no real AWS/DB/network.

## Not yet done in this scaffold

- `infra/` CDK stack (Fargate + Aurora + this service's own
  `order-events-q` + DLQ + the `execute-api:Invoke` grant for the
  SigV4-signed User Service call) — MA-25 implementation plan Step 5.
  It must also create the sweep alarms (MA-143 §6), from log metric
  filters on `$.metric`: any `sweep.subscription_order.escalated` or
  `sweep.checkout.escalated` in 5 min → ops notification; any
  `sweep.*.charged_after_cutoff` or `sweep.settle.escalated_charged` →
  ops notification (money taken, delivery at risk);
  `sweep.run_failed` ≥ 3 in 15 min → ops notification.
- `services/local-dev` wiring (`docker-compose.yml` entry, database
  bootstrap, queue/rule bootstrap) — same step.
- The checkout's IAM grant for Cart's internal routes (Cart stack's
  `internal_caller_role_arns`) — same manual cross-stack step as User's.
