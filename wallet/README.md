# Wallet Service

Prepaid wallet balance + immutable ledger. Auto-provisions on
registration, credits recharges idempotently, and serves the balance +
passbook read APIs.

Implements the MA-1 `wallet-auto-provision` baseline **and** the MA-24
recharge slice (`specs/services/tasks/MA/MA-100/MA-127.md`). MA-1 was
never implemented separately, so this service is the first Wallet
Service scaffold and carries both.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/wallet/me` | Cognito JWT | Balance (paise), status, recharge bounds — the MA-24 shape MA-125 consumes |
| GET | `/wallet/me/status` | Cognito JWT | **MA-1 legacy body** `{walletId, status, balance (whole rupees), currency}` — unchanged for MA-1's registration screen |
| GET | `/wallet/me/transactions` | Cognito JWT | Paged (keyset), newest-first ledger — the contract MA-27 renders |
| POST | `/wallet/me/retry` | Cognito JWT | MA-1 replay of auto-provision |
| GET | `/wallet/internal/limits` | SigV4 (VPC-only) | Recharge min/max for Payment Service (MA-126) |

## Events

- **Consumes** (`wallet-events-q`): `UserRegistered` → create wallet;
  `PaymentConfirmed` where `detail.purpose = WALLET_RECHARGE` → credit the
  ledger.
- **Publishes** (transactional outbox → EventBridge): `WalletCreated`,
  `WalletCredited`.

## Data (Aurora `wallet`)

`wallets(id, user_id UNIQUE, balance_paise BIGINT, currency, status, …)`,
`ledger_entries(id, wallet_id, type, amount_paise (signed),
balance_after_paise, ref TEXT UNIQUE, correlation_id, created_at)`,
`outbox(…)`. Money is integer paise everywhere. The `ref` UNIQUE +
`SELECT … FOR UPDATE` on the wallet row make recharge crediting
exactly-once.

## Local development

Listens on `:8006`. FastAPI + a background SQS consumer thread, same
single-deployable shape as `catalog`/`inventory`.

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python src/main.py                 # HTTP + wallet-events consumer
python src/handlers/outbox_publisher.py   # drains the outbox to EventBridge
```

## Tests

```
pytest
```

SQLite in-memory stands in for Aurora (documented fidelity gap, same as
`catalog`); no real AWS/DB/network.

## Not yet done in this scaffold

- `infra/` CDK stack (Fargate service, Aurora ref, `wallet-events-q` +
  DLQ, `UserRegistered` rule, SSM export of the queue ARN for the Payment
  stack's recharge rule).
- `services/local-dev` wiring (port 8006, DB `milkful_wallet`, moto
  queue + rules) — plan Step 5.
