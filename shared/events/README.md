# Shared event schemas

Authoritative JSON Schema (draft 2020-12) definitions for the EventBridge
`detail` payloads on the `milkful-events` bus.

| Schema | Producer | Consumers |
|--------|----------|-----------|
| `PaymentConfirmed` | Payment Service (`milkful.payment`, MA-126) | Wallet Service `wallet-events-q` (recharge, filtered on `detail.purpose = WALLET_RECHARGE`); Order Service (future, `ORDER`); Reporting (catch-all) |
| `PaymentFailed` | Payment Service | Notification Service; Reporting |
| `WalletCredited` | Wallet Service (`milkful.wallet`, MA-127) | Notification Service (optional push); Reporting |

Each service that produces or consumes one of these events validates the
payload against the matching schema in its own test suite via
`from shared.events import load_schema`. A contract change touches **only**
these files.

Introduced for MA-24 (Wallet & Recharge). See `services/tasks/MA/MA-99/MA-126.md` §8
for the full contract discussion.
