# User Service

Registration orchestration — persists customer profile, delivery
address(es), preferred slot, and legal consents after OTP verification,
then signals downstream services via `UserRegistered`. Implements
[MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1) / backend
story [MA-93](https://milkfuldairyindia.atlassian.net/browse/MA-93), per
spec `specs/services/tasks/MA/MA-1/user-registration-api.md`.

Wallet auto-provision ([MA-100](https://milkfuldairyindia.atlassian.net/browse/MA-100))
is out of scope — this service only publishes the event; nothing here
waits for a wallet to exist.

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| POST | `/users/register` | FR-1 | Cognito JWT (API Gateway authorizer) |
| GET | `/delivery/slots?zoneId=` | FR-2 | Cognito JWT |

`sub` and `mobile` are read from the API Gateway JWT authorizer's
verified claims — never trusted from the request body.

## Architecture decisions flagged for review

1. **Delivery slots read from a local `zone_slots` table, not a live
   Inventory call** — a plan deviation from the original approach.
   Inventory's real API (MA-95) only accepts pincode/lat/lng and returns
   slots embedded in a serviceability response; there's no zoneId-keyed
   "get slots" endpoint to call against. The spec explicitly allows
   "cached from Inventory or local replica" — this uses the local
   replica option. **How `zone_slots` gets populated or kept in sync
   with Inventory's actual zone config is not wired** — a real gap
   needing a human decision (poll Inventory periodically? consume a
   zone-sync event? manual seed data?).
2. **Cross-stack Cognito reference is a placeholder.** This service
   needs MA-92's real Cognito User Pool ID/Client ID for the JWT
   authorizer and the attribute-sync IAM policy; MA-92's stack doesn't
   export them. `infra/app.py` passes literal placeholder strings — a
   human must wire a real cross-stack reference (SSM Parameter Store
   export from MA-92's stack is the lowest-friction option) before this
   is deployable.
3. **`custom:default_pincode` doesn't exist on MA-92's actual Cognito
   pool schema.** Custom attributes are creation-time-only — this
   service's `cognito_attribute_adapter` is correct against the
   *intended* schema, but real calls will fail until MA-92's stack is
   updated to add it. A cross-PR follow-up, not fixed here.
4. **Third dedicated VPC.** MA-92, MA-95, and now this service have each
   provisioned their own VPC. This is the third time this pattern has
   repeated — worth an explicit shared-VPC (or peering / Transit
   Gateway) architecture decision soon, not solved here.
5. **`DATABASE_URL` composition not wired**, same gap and same reason as
   MA-95's stack (Aurora's generated secret has separate JSON fields;
   Lambda `Secret`/env-var injection is one-field-at-a-time).
6. **Inventory reachability is a placeholder URL.** `inventory_client_adapter`
   is correct and fully tested (via `responses`), but User Service's
   Lambda and Inventory's internal ALB live in separate,
   independently-provisioned VPCs — real connectivity needs the same
   shared-VPC/peering decision as point 4.
7. **Cognito sync failure is swallowed, not raised**, after the DB
   transaction already committed — registration itself (user/address/
   consent persisted, outbox written) is what must not fail; a stale
   Cognito profile attribute is a lesser, recoverable problem. Logged,
   not silent.

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate  # or .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
pytest                          # full suite: unit + integration + infra, no AWS/DB needed

cd infra
pip install -r requirements.txt
cdk synth                       # no AWS credentials, no Docker
```

## Testing approach

Fully offline — **no AWS credentials, no Docker, no real Postgres**:

- SQLite in-memory (`StaticPool`, `check_same_thread=False`, same
  reasoning as MA-95) stands in for Aurora.
- `moto` mocks Cognito and EventBridge.
- `responses` mocks the Inventory HTTP call.
- `tests/infra/test_user_stack.py` uses `Template.from_stack` assertions
  against a real `cdk synth`.

**Known moto fidelity gap** (discovered while testing
`cognito_attribute_adapter`): unlike real Cognito, moto does not reject
`AdminUpdateUserAttributes` calls for a custom attribute absent from the
pool's schema — so the exact failure mode flagged in decision #3 above
couldn't be demonstrated with a failing test against moto; documented
in `tests/unit/adapters/test_cognito_attribute_adapter.py` instead of
faked.

## What still needs a human

- `cdk bootstrap`/`cdk deploy` to a real AWS account.
- A real cross-stack reference to MA-92's Cognito pool (flagged decision #2).
- Adding `custom:default_pincode` to MA-92's Cognito pool schema (decision #3).
- Resolving the shared-VPC/peering question across MA-92/MA-95/MA-93
  (decision #4/#6) so this service can actually reach Aurora, Inventory,
  and (indirectly) Cognito's VPC endpoints if used.
- Wiring real `DATABASE_URL` composition from the injected secrets.
- Deciding how `zone_slots` gets populated/synced (decision #1).
- Measuring the NFR "Register p95 < 1s" against a deployed environment.

## Deferred / tech debt

- Wallet auto-provision (MA-100) — this service only publishes
  `UserRegistered`; nothing here creates or polls a wallet.
- Concurrent duplicate-registration race: the idempotency check is a
  SELECT-then-INSERT within one transaction, which handles sequential
  duplicate calls correctly (tested), but a true concurrent race on the
  same `cognito_sub` could still raise an `IntegrityError` on the unique
  constraint rather than gracefully returning the existing user — not
  handled, left as a known edge case.
