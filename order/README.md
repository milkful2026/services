# Order Service

Turns a due subscription delivery into a real, paid Order: quotes the
current price via Pricing Service, debits the customer's wallet, and
records the outcome.

Implements `specs/services/tasks/MA/MA-25/MA-132.md` (MA-99/MA-25). New
service — no prior scaffold.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/orders/me` | Cognito JWT | Paged (keyset), newest-first, optionally filtered by `subscriptionId` (FR-3) |
| GET | `/orders/{id}` | Cognito JWT | One order's full detail (FR-3) |

No public write endpoints — every order is consumer-created from
`SubscriptionOrderDue`, never a direct client `POST`.

## Events

- **Consumes** (`order-events-q`, owned by this service): `SubscriptionOrderDue`
  from Subscription Service (MA-131).
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
confirmed_at)` with `UNIQUE(subscription_id, delivery_date)` and
`CHECK(amount_paise > 0)`, plus `outbox(…)`.

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
