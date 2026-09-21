# Subscription Service

Recurring-order lifecycle: create/pause/resume/stop/skip/edit a
subscription, and a Daily Run that decides what's due each day and tells
Order Service so.

Implements `specs/services/tasks/MA/MA-25/MA-131.md` (MA-100/MA-25). New
service — no prior scaffold.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/subscriptions` | Cognito JWT | Create (FR-1); same-day `SubscriptionOrderDue` emitted synchronously if `startDate == today`, still before cut-off |
| POST | `/subscriptions/{id}/pause` | Cognito JWT | Pause a date range, or indefinitely (FR-2) |
| POST | `/subscriptions/{id}/resume` | Cognito JWT | Clear the pause window (FR-3) |
| POST | `/subscriptions/{id}/stop` | Cognito JWT | Terminal, idempotent (FR-4) |
| POST | `/subscriptions/{id}/skip` | Cognito JWT | Skip one due date, before its cut-off (FR-5) |
| POST | `/subscriptions/{id}/edit` | Cognito JWT | Quantity/schedule — immediate or `pending_edit` depending on cut-off (FR-6) |
| GET | `/subscriptions` | Cognito JWT | List mine, `nextDeliveryDate` computed (FR-9) |
| GET | `/subscriptions/{id}` | Cognito JWT | One subscription's detail (FR-9) |
| POST | `/internal/run-daily` | network-level (VPC-only, no JWT) | The Daily Run — EventBridge Scheduler target in prod, a local-dev script here |

## Events

- **Publishes** (transactional outbox → EventBridge): `SubscriptionOrderDue`
  — once per due subscription, consumed by Order Service's `order-events-q`.
- **Consumes**: none — this service owns no SQS consumer; the Daily Run is
  triggered externally via `POST /internal/run-daily`.

## Data (Aurora `subscription`)

`subscriptions(id, user_id, product_id, quantity, schedule JSONB,
slot_id, status, start_date, pause_from, pause_until, pending_edit JSONB,
created_at, updated_at)`, `subscription_skips(subscription_id,
skipped_date)`, `subscription_run_log(subscription_id, delivery_date)`
UNIQUE (the Daily Run's own idempotency backstop), `outbox(…)`.

## Local development

Listens on `:8008`. FastAPI only — no background thread (nothing to
consume); local-dev emulates EventBridge Scheduler with a cron-like
script hitting `POST /internal/run-daily` (see `services/local-dev/`).

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python src/main.py                        # HTTP only
python src/handlers/outbox_publisher.py   # drains the outbox to EventBridge
```

## Tests

```
pytest
```

SQLite in-memory stands in for Aurora (documented fidelity gap, same as
every other service here); no real AWS/DB/network.

## Not yet done in this scaffold

- `infra/` CDK stack (Fargate + Aurora + the EventBridge Scheduler rule +
  the `SubscriptionOrderDue` → `order-events-q` rule/permission) — MA-25
  implementation plan Step 5.
- `services/local-dev` wiring (`docker-compose.yml` entry, database
  bootstrap, the Scheduler-emulation script) — same step, done alongside
  Order Service.
