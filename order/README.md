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

## Data (Aurora `order`)

`orders(id, user_id, subscription_id, product_id, quantity,
amount_paise, delivery_date, status, failure_reason, created_at,
confirmed_at, source, checkout_id)` with `UNIQUE(subscription_id,
delivery_date)`, `UNIQUE(checkout_id)`, `CHECK(amount_paise >= 0)` and
the `orders_source_shape` CHECK; `order_items(order_id, line_no,
product_id, quantity)`; `checkouts(…)` with `UNIQUE(user_id,
idempotency_key)` and a partial unique index allowing one `IN_PROGRESS`
checkout per user (`migrations/0002_checkout.sql`); plus `outbox(…)`.

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
- `services/local-dev` wiring (`docker-compose.yml` entry, database
  bootstrap, queue/rule bootstrap) — same step.
- A reconciliation sweep for an order stuck in `CREATED` past SQS's
  visibility timeout — MA-132 §11 explicitly defers this; redelivery is
  the only recovery path in this pass.
- A sweep that resumes checkouts left `IN_PROGRESS` by a client that
  never retried (MA-136 §11). Until it exists, such a checkout is only
  finished when the user next checks out (it no longer blocks them once
  it's been untouched for 2 minutes), so a paid-but-unfinished checkout
  can sit with its subscriptions not yet created. **Needed before
  production.**
- The checkout's IAM grant for Cart's internal routes (Cart stack's
  `internal_caller_role_arns`) — same manual cross-stack step as User's.
