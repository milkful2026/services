# Shared event schemas

Authoritative JSON Schema (draft 2020-12) definitions for the EventBridge
`detail` payloads on the `milkful-events` bus.

| Schema | Producer | Consumers |
|--------|----------|-----------|
| `PaymentConfirmed` | Payment Service (`milkful.payment`, MA-126) | Wallet Service `wallet-events-q` (recharge, filtered on `detail.purpose = WALLET_RECHARGE`); Order Service (future, `ORDER`); Reporting (catch-all) |
| `PaymentFailed` | Payment Service | Notification Service; Reporting |
| `WalletCredited` | Wallet Service (`milkful.wallet`, MA-127) | Notification Service (optional push); Reporting |
| `SubscriptionOrderDue` | Subscription Service (`milkful.subscription`, MA-131) | Order Service `order-events-q` |
| `OrderConfirmed` | Order Service (`milkful.order`, MA-132) | Notification Service (future); Reporting |
| `OrderPaymentFailed` | Order Service (`milkful.order`, MA-132) | Notification Service (future); Reporting |
| `WalletDebited` | Wallet Service (MA-130 debit extension) | Reporting; Notification Service (future) |
| `WalletLowBalance` | Wallet Service (MA-130 debit extension) | Notification Service (future); Reporting |
| `WalletRefunded` | Wallet Service (`milkful.wallet`, MA-153) | Notification Service (future, MA-103); Reporting (future, MA-104) |
| `OrderCancelled` | Order Service (`milkful.order`, MA-154) | Notification Service (MA-103), Reporting (MA-104), Delivery Service (MA-102) — all future. **Not** Wallet: the refund is a synchronous call (MA-153) |

Each service that produces or consumes one of these events validates the
payload against the matching schema in its own test suite via
`from shared.events import load_schema`. A contract change touches **only**
these files.

Introduced for MA-24 (Wallet & Recharge). See `services/tasks/MA/MA-99/MA-126.md` §8
for the full contract discussion. Extended for MA-25 (Subscription Module) — see
`services/tasks/MA/MA-25/MA-131.md` §4/§6 and `services/tasks/MA/MA-25/MA-132.md` §8. Extended for MA-32 (Order Cancellation) — see `services/tasks/MA/MA-32/MA-153.md` §4
and `services/tasks/MA/MA-32/MA-154.md` §4.
