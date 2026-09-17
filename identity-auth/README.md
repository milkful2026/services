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

It also implements **Admin Identity, RBAC & Session Security**
([MA-129](https://milkfuldairyindia.atlassian.net/browse/MA-129), spec
`specs/services/tasks/MA/MA-47/MA-129.md`) — a second, separate Cognito
"Admin Pool" (password + required TOTP MFA) with its own Aurora
`admin_user` table, for staff/ops accounts. This is purely additive: it
does not touch the consumer OTP pool/flows above in any way.

## Endpoints

### Consumer (MA-1 / MA-21)

| Method | Path | Spec | Auth |
|--------|------|------|------|
| POST | `/v1/auth/otp/send` | MA-1 FR-1 | none (pre-auth) |
| POST | `/v1/auth/otp/verify` | MA-1 FR-2 | none (pre-auth) |
| POST | `/v1/auth/social` | MA-1 FR-3 | none (pre-auth) |
| POST | `/v1/auth/token/refresh` | MA-1 FR-4 | none (pre-auth) |
| POST | `/v1/auth/login/otp/send` | MA-21 FR-1 | none (pre-auth) |
| POST | `/v1/auth/login/otp/verify` | MA-21 FR-2 | none (pre-auth) |
| POST | `/v1/auth/logout` | MA-21 FR-3 | Cognito JWT |

### Admin (MA-129)

| Method | Path | Spec | Auth |
|--------|------|------|------|
| POST | `/v1/admin/auth/login` | FR-1 | none (pre-auth) |
| POST | `/v1/admin/auth/2fa/verify` | FR-2 | none (pre-auth) |
| POST | `/v1/admin/users` | FR-3 | Admin JWT, SuperAdmin group |
| GET | `/v1/admin/users` | FR-4 | Admin JWT, SuperAdmin group |
| PATCH | `/v1/admin/users/{id}` | FR-4 | Admin JWT, SuperAdmin group |
| POST | `/v1/admin/users/{id}/deactivate` | FR-4 | Admin JWT, SuperAdmin group |
| POST | `/v1/admin/users/{id}/reactivate` | FR-4 | Admin JWT, SuperAdmin group |

"Admin JWT" routes sit behind a custom Lambda REQUEST authorizer
(`src/handlers/admin_authorizer_handler.py`) — not the Cognito JWT
authorizer the consumer `/v1/auth/logout` route uses — because it also
re-checks Aurora's live status/role/IP-allowlist on every call (FR-5).
SuperAdmin-only enforcement happens twice: once implicitly (every
protected route requires *a* valid Admin Pool JWT) and again explicitly
inside `AdminUserService` itself (never trusting a client-supplied role,
services/README.md §5b) — the authorizer does not itself check group
membership beyond "is this admin Active".

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

## MA-129 (Admin Identity, RBAC & Session Security) — architecture decisions flagged for review

Same spirit as the numbered list above: these shipped a working,
spec-faithful implementation, but are genuine calls that need explicit
sign-off, not silent defaults. Also see the spec file itself (§11/§12)
for the decisions the spec *already* flags as needing architect
sign-off (the second-Cognito-pool decision, authorizer-caching
trade-off, and bootstrap procedure) — the items below are additional
ones surfaced during implementation.

1. **Spec gap: nothing transitions `admin_user.status` from Pending to
   Active.** FR-3 creates an admin as Pending; FR-4's reactivate only
   covers Deactivated -> Active. No endpoint in this spec describes how
   a newly-invited admin becomes Active after completing password-set +
   TOTP enrollment, and FR-1 explicitly refuses login for a Pending
   account. As implemented, a newly-created admin literally cannot ever
   log in without an out-of-band Aurora update — this is a real gap,
   not an oversight, and needs a decision (a self-service "accept
   invitation" endpoint most likely, not yet specified) before this
   feature is actually usable end-to-end.
2. **Spec gap: JWT refresh/logout has no concrete endpoint.** Spec §3's
   Scope lists "JWT issuance/refresh/logout" as in-scope, but §4's
   FR-1..FR-6 never define a `/v1/admin/auth/refresh` or
   `/v1/admin/auth/logout` route, and the implementation task's own
   endpoint list omits them too. Not implemented. The Admin Pool app
   client still allows Cognito's standard `REFRESH_TOKEN_AUTH` flow (CDK
   enables it on every client by default), so `portal-ui` could
   technically call Cognito's public `InitiateAuth` directly for a
   refresh — but that's an assumption, not a confirmed contract, and
   there is no admin-specific logout (single-refresh-token revocation)
   endpoint at all yet.
3. **Login challengeToken and 2FA lockout counters live in Redis, not a
   new DynamoDB table.** Structurally these are the same kind of
   ephemeral, short-TTL, single-use-on-success record `otp_requests`
   already models for the consumer flow, but Redis's native key TTL was
   reused instead (this service's existing ElastiCache instance) rather
   than adding a second AWS resource for the same shape of problem.
4. **`maxConcurrentSessions` LRU eviction requires storing refresh
   tokens server-side in Redis** (`admin_session_registry.py`). Cognito
   has no API to enumerate or selectively revoke a user's active refresh
   tokens by age — only `AdminUserGlobalSignOut`, which revokes all of
   them. Implementing "evict the oldest" therefore means this service
   holds bearer-equivalent credentials in Redis purely so it can later
   call `RevokeToken` on the oldest one. This is a real secret-handling
   trade-off, not a casual choice — flagged explicitly for security
   review rather than silently accepted.
5. **A role change invalidates ALL of the target's tracked sessions via
   `AdminUserGlobalSignOut`, not "the current one."** Spec §9 says a
   role change should invalidate "the target admin's current refresh
   token" (singular), but Cognito has no concept of "the current" token
   distinct from any other active one. `AdminUserGlobalSignOut` is the
   correct primitive for the stated trade-off anyway (it only breaks
   future `REFRESH_TOKEN_AUTH` calls — an already-issued, unexpired
   access token's JWT signature still validates until natural expiry,
   which is exactly the "stale role claim persists at most one
   access-token lifetime" behavior spec §9 describes as deliberate).
6. **No safeguard against locking out the only Super-Admin via IP
   allowlist misconfiguration** (spec §9/§12 Q4) — spec explicitly says
   "No automated safeguard in this spec," so none was added.
   `admin_user_repository.count_active_super_admins()` exists and is
   tested, ready for a future guard, but is currently unused — a
   deliberate "hook present, not wired" choice rather than half-
   implementing an unspecified feature.
7. **New admin endpoints use HTTP 422 for domain validation
   (`AdminValidationError`/`InvalidRoleError`/`InvalidCidrError`), while
   the pre-existing consumer endpoints' `ValidationError` uses 400.**
   services/README.md §5c's error table specifies 422 for domain
   validation; the consumer flow's 400 predates this feature and is left
   untouched (additive-only constraint) — this is an inherited
   inconsistency across the same service, not a new one introduced here.
8. **Admin Pool token TTLs (15 min access/ID, 1 day refresh) and
   disabled authorizer-result caching** are concrete numbers picked to
   answer spec §12 Q2/Q5, which explicitly leave them open. See the CDK
   stack's own module docstring (points 8-9) for the reasoning — both
   need explicit architect confirmation, not just an absence of
   objections.
9. **The admin authorizer Lambda needs internet egress it doesn't have.**
   `admin_authorizer_handler.py` fetches Cognito's public JWKS endpoint
   via plain HTTPS (same pattern as `social_jwks_adapter.py` for
   Google/Apple), but this stack's VPC has `nat_gateways=0` — nothing
   placed in it can reach the internet at all. This is a *pre-existing*
   gap for `social_auth_fn` (out of scope to fix here — additive-only
   constraint on the consumer flow), but the new authorizer inherits it.
   See "What still needs a human" below.
10. **`admin_cognito_adapter.py` fails closed if Cognito's
    `AdminInitiateAuth` response doesn't include a `SOFTWARE_TOKEN_MFA`
    challenge** — treated as a pool misconfiguration (502) rather than a
    successful single-factor login, since a correctly-configured Admin
    Pool (MFA required) should never skip the challenge. This also means
    moto-backed tests must monkeypatch this method (moto doesn't emulate
    the challenge at all — see "Known test-fidelity gaps" below);
    real-Cognito verification of this exact code path still needs a
    human.

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

**MA-129 adds one more:** moto does **not** implement the
`SOFTWARE_TOKEN_MFA` challenge/response flow at all —
`admin_initiate_auth` returns `AuthenticationResult` directly even when
the pool has `MfaConfiguration="ON"` and the user has no TOTP device
associated (confirmed by direct experiment against moto, not assumed).
`test_admin_cognito_adapter.py` and `test_admin_flow.py` (integration)
therefore monkeypatch `AdminCognitoAdapter.admin_password_auth` /
`.respond_to_mfa_challenge` (or the underlying boto3 calls) to simulate
Cognito's real two-step behavior with a fixed test TOTP code. This means
**no test in this repository exercises real TOTP secret generation,
`AssociateSoftwareToken`/`VerifySoftwareToken` enrollment, or a real
6-digit code's validation** — that whole surface needs a human against a
real (or LocalStack) pool before this ships.

## What still needs a human

- `cdk bootstrap` / `cdk deploy` to a real AWS account.
- Provisioning ElastiCache for real and validating Lambda→Redis
  connectivity from inside the VPC.
- **Lambda dependency packaging.** `Code.from_asset` here bundles only
  `src/` — no third-party dependencies (pydantic, bcrypt, PyJWT, redis,
  cachetools, requests, aws-lambda-powertools, sqlalchemy,
  psycopg2-binary; boto3 is provided by the Lambda runtime). A Lambda
  Layer (or switching to Docker-bundled `PythonFunction` from
  `aws-cdk.aws-lambda-python-alpha`) must be added before this is
  actually deployable — deferred because Docker bundling may not be
  available in every environment this needs to `cdk synth` in.
  `psycopg2-binary` in particular typically needs a Linux-compatible
  wheel/layer, not whatever wheel the packaging machine's own OS pulls.
- Registering real Google/Apple OAuth client IDs and testing FR-3
  against their live JWKS + live tokens.
- Confirming `OtpRequested` is actually consumed once the Notification
  service exists.
- Measuring the NFR "verify p95 < 500ms" against a deployed environment.
- Tuning/validating the WAF rate-based rule against real traffic.

### MA-129 (Admin Identity, RBAC & Session Security) specifics

- **Aurora provisioning specifics**: `_build_admin_database` in
  `infra/identity_auth/identity_auth_stack.py` provisions a brand-new
  Aurora **PostgreSQL Serverless v2** cluster (`AdminAuroraCluster`,
  engine version 16.4, `serverless_v2_min_capacity=0.5` /
  `max_capacity=2`, one `Writer` instance, `default_database_name=
  "admin"`, credentials via `rds.Credentials.from_generated_secret(
  "identity_auth_admin_service")`, `RemovalPolicy.RETAIN`) inside this
  stack's existing VPC's `PRIVATE_ISOLATED` subnets — reusing the VPC
  created for the consumer OTP flow's Redis, not a new one. A human
  still needs to: run `migrations/0001_admin_user.sql` against the real
  cluster once it exists (no automatic migration runner is wired — same
  as `services/user`'s own migrations); confirm the Serverless v2
  min/max capacity numbers against actual admin traffic once there is
  any; and decide whether `RemovalPolicy.RETAIN` is really desired for a
  non-production environment (it deliberately prevents `cdk destroy`
  from dropping the database).
- **NAT Gateway / internet egress for `admin_authorizer_handler.py`**
  (and, pre-existing, `social_auth_fn`) — this stack's VPC has
  `nat_gateways=0`; nothing in it can reach the public internet, but the
  authorizer must reach Cognito's public JWKS endpoint. A human needs to
  choose: add a NAT Gateway + `PRIVATE_WITH_EGRESS` subnet for just this
  Lambda (cost trade-off), or an alternative (e.g. a small
  internet-facing JWKS-caching Lambda the authorizer calls internally,
  or restructuring where JWT verification happens). Not resolved here.
- **The bootstrap script itself**
  (`scripts/bootstrap_super_admin.py`) needs a human to actually run it
  once, with real `--admin-pool-id` / `--database-url` / AWS
  credentials, after the stack is deployed — it is deliberately not
  wired into any CDK custom resource or CI step (see the script's own
  docstring for why).
- **The Pending -> Active gap** (flagged decision #1 above) blocks any
  newly-invited admin from ever completing login through this API alone
  — needs a product/spec decision, not an engineering guess.
- **TOTP enrollment UX** is entirely unspecified/unimplemented here —
  `AssociateSoftwareToken`/`VerifySoftwareToken` are Cognito APIs a human
  must exercise against a real pool; nothing in this backend currently
  calls them (the assumption is `portal-ui`/MA-128 drives enrollment
  directly against Cognito, but this is unconfirmed).

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
- **MA-129**: no Pending -> Active transition endpoint (spec gap #1
  above); no admin refresh/logout endpoint (spec gap #2); no last-
  Super-Admin IP-lockout safeguard (spec explicitly defers this, #6
  above, though `count_active_super_admins()` exists ready to support
  one); real TOTP enrollment/verification is entirely unexercised by
  this offline test suite (see "Known test-fidelity gaps"); NAT Gateway
  / internet egress for the admin authorizer is unresolved (see "What
  still needs a human").
