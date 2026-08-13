# Identity & Auth Service

Cognito-backed OTP registration and login, optional Google/Apple social
auth, and JWT issuance/refresh/revocation for Milkful's mobile app.
Implements the **registration-flow** endpoints for
[MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1) / backend story
[MA-92](https://milkfuldairyindia.atlassian.net/browse/MA-92) (spec
`specs/services/tasks/MA/MA-1/identity-auth-registration.md`), and the
**login-flow** endpoints for
[MA-21](https://milkfuldairyindia.atlassian.net/browse/MA-21) (spec
`specs/services/tasks/MA/MA-21/identity-auth-login.md`).

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| POST | `/v1/auth/otp/send` | MA-1 FR-1 | none (pre-auth) |
| POST | `/v1/auth/otp/verify` | MA-1 FR-2 | none (pre-auth) |
| POST | `/v1/auth/social` | MA-1 FR-3 | none (pre-auth) |
| POST | `/v1/auth/token/refresh` | MA-1 FR-4 | none (pre-auth) |
| POST | `/v1/auth/login/otp/send` | MA-21 FR-1 | none (pre-auth) |
| POST | `/v1/auth/login/otp/verify` | MA-21 FR-2 | none (pre-auth) |
| POST | `/v1/auth/logout` | MA-21 FR-3 | Cognito JWT |

## Architecture decisions flagged for review

These were made to ship a working, spec-faithful implementation, but are
genuine architecture calls that haven't had explicit sign-off — surfaced
here and in the PR description rather than silently assumed:

1. **Token issuance mechanism.** This service owns OTP verification itself
   (DynamoDB, not Cognito's native custom-auth-challenge Lambda triggers).
   After we independently verify an OTP or a social idToken, we mint
   Cognito tokens via `AdminCreateUser` → `AdminSetUserPassword` (random,
   permanent, single-use) → `AdminInitiateAuth`. The password is
   generated, set, immediately consumed, and never stored.
2. **Cognito Username scheme.** The pool uses `UsernameAttributes =
   [phone_number, email]` — Username is a *literal* phone number or email,
   not an alias on a generated ID. This is required because real Cognito
   rejects arbitrary Usernames once `UsernameAttributes` is set
   (`InvalidParameterException: Username should be either an email or a
   phone number` — confirmed against moto, which matches real AWS here).
   Consequence: a federated (social) user with no phone yet is created
   with `Username = email`; the provider's own `sub` is stored as a
   custom attribute (`custom:google_sub` / `custom:apple_sub`) for
   audit/support only, never as a lookup key — `ListUsers`' `Filter` does
   not support searching by custom attributes at all.
3. **Social-to-mobile account linking is not implemented.** Spec FR-3's
   flagged G1 / Open Question Q2 ("social account merge UX") is an
   unresolved product decision. `find_or_create_federated_user` only ever
   matches an *email-username* Cognito record created by a prior social
   login — it cannot find a *phone-username* record from OTP registration
   even if that user's email matches. `partial_token` (returned when
   `requiresMobileVerification: true`) is a placeholder identifier, not a
   signed credential, and `POST /v1/auth/otp/verify`'s request contract
   has no field to accept it back — this whole branch is inert until the
   merge UX is scoped. To avoid silently creating a second, disconnected
   identity in the meantime, `find_or_create_federated_user` now checks
   for an existing user with a matching `email` attribute before creating
   one and raises `SocialAccountConflictError` (409, `mergeInstructionCode:
   "CONTACT_SUPPORT"`) if found — a conservative stopgap, not the merge UX
   itself.
4. **EventBridge bus.** `OtpRequested` publishes to the account **default**
   bus, not a new named bus — no shared `milkful-domain-events` bus exists
   yet. The CDK stack's rule target is a CloudWatch log group (not a
   guess at the Notification service's queue, which doesn't exist yet
   either).
5. **CDK scoping.** Kept self-contained under `infra/` in this service
   rather than creating a new top-level `services/infrastructure/` —
   that folder is documented (`services/README.md` §2) as *cross-service*
   shared IaC, and creating it as a side effect of this one service's
   ticket would be an unapproved architecture decision.
6. **Dedicated VPC.** A small VPC is created here purely so this
   service's Lambdas can reach ElastiCache — no shared VPC exists yet.
7. **IAM scoping limitation.** Several `cognito-idp:Admin*` /
   `InitiateAuth` actions do not support resource-level IAM conditions —
   AWS requires `Resource: "*"` for them. Least-privilege here means
   action-level scoping only (just the specific admin actions this
   service calls), not resource-ARN scoping — a real AWS limitation, not
   an oversight.
8. **DynamoDB schema additions.** `status` and `lastSentAt` fields exist
   beyond the spec's literal `otp_requests` schema. They're required to
   correctly implement "duplicate send while valid OTP active" (needs
   `lastSentAt` to compute the resend cooldown) and to distinguish a
   locked record from one merely past its TTL but not yet deleted (DynamoDB
   TTL deletion isn't immediate).
9. **`purpose` gates rate-limit/lock keys and the duplicate-send lookup,
   not OTP verification itself.** `verify_otp` doesn't check that a
   record's `purpose` matches the endpoint it was submitted to — a
   REGISTER-purpose OTP could technically be consumed via
   `/login/otp/verify` (or vice versa) if a client had that requestId.
   This isn't a security gap (the OTP is still a correctly-hashed,
   single-use, attempt-limited code sent to that specific phone —
   `purpose` isolates rate-limit budgets and SMS templates, not
   authorization), but it's a deliberate scope boundary worth being
   explicit about rather than silent on.

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate  # or .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
pytest                          # full suite: unit + integration + infra, no AWS credentials needed
```

Infra-only (if you don't want CDK deps in your main venv):

```bash
cd infra
pip install -r requirements.txt
cdk synth
```

## Testing approach

Everything runs offline — **no AWS credentials, no Docker, no real
Redis**:

- `moto` mocks DynamoDB, Cognito, and EventBridge for adapter/integration
  tests.
- `fakeredis` stands in for ElastiCache in rate-limiter and integration
  tests.
- `responses` mocks the Google/Apple JWKS HTTP endpoints; test JWTs are
  signed with a locally generated RSA keypair (`cryptography`).
- `freezegun` gives integration tests deterministic control over OTP
  expiry/resend-cooldown timing.
- CDK: `tests/infra/test_identity_auth_stack.py` uses
  `Template.from_stack` assertions against a real `cdk synth` — this
  needs Node.js (for the CDK CLI's JSII bridge) but no AWS account.

**Known test-fidelity gaps:** moto's `cognito-idp` mock has limited
fidelity — tokens from `AdminInitiateAuth`/`InitiateAuth` are
fake/unsigned-looking, some admin APIs behave more leniently than real
Cognito (e.g. password policy enforcement isn't fully emulated), and
**`RevokeToken` isn't implemented at all** (moto raises a raw
`NotImplementedError`, not even a `ClientError`). `test_cognito_adapter.py`
and the login integration test verify `revoke_token`'s call shape via
monkeypatch instead of exercising it through moto. None of this
validates real Cognito token semantics — that needs a human against a
real (or LocalStack) pool.

## What still needs a human

- `cdk bootstrap` / `cdk deploy` to a real AWS account.
- Provisioning ElastiCache for real and validating Lambda→Redis
  connectivity from inside the VPC.
- **Lambda dependency packaging.** `Code.from_asset` here bundles only
  `src/` — no third-party dependencies (pydantic, bcrypt, PyJWT, redis,
  cachetools, requests, aws-lambda-powertools; boto3 is provided by the
  Lambda runtime). A Lambda Layer (or switching to Docker-bundled
  `PythonFunction` from `aws-cdk.aws-lambda-python-alpha`) must be added
  before this is actually deployable — deferred because Docker bundling
  may not be available in every environment this needs to `cdk synth` in.
- Registering real Google/Apple OAuth client IDs and testing FR-3
  against their live JWKS + live tokens.
- Confirming `OtpRequested` is actually consumed once the Notification
  service exists.
- Measuring the NFR "verify p95 < 500ms" against a deployed environment.
- Tuning/validating the WAF rate-based rule against real traffic.

## Deferred / tech debt

- Social-to-mobile account linking (see flagged decision #3 above).
- `partial_token` is a non-functional placeholder (see #3) pending the
  product decision on social/mobile merge UX.
- "Log out everywhere" (revoking every device, not just the calling one)
  is explicitly out of MA-21's scope per that spec's own risk register —
  only per-device logout (`RevokeToken`) is built.
- Real `RevokeToken` behavior (idempotency on an already-revoked token,
  exact error codes for a malformed token) needs human verification
  against real/LocalStack Cognito — moto doesn't implement the action at
  all (see "Known test-fidelity gaps" above).
