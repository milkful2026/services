# Milkful Services — Engineering Guidelines

This document defines the **coding principles, architecture standards, and engineering guardrails**
for building Milkful backend API services on **AWS**.

The goal is a consistent development model for service boundaries, data ownership, event contracts,
deployment practices, and code quality across all microservices.

**Status:** scaffold only — service code is not yet implemented. Treat this file as the target
standard for implementation agents and human engineers.

**Authoritative architecture:** `milkful2026/milkful-app` → `docs/design/milkful-well-architected.md`
(+ HLD / LLD / messaging draw.io diagrams).

---

# 0. Technology Stack

| Layer | Choice |
|-------|--------|
| Cloud | AWS (multi-AZ VPC, private subnets, VPC endpoints) |
| Edge / BFF | API Gateway (+ Cognito JWT authorizer) |
| Compute | Hybrid: **Lambda** (spiky / event) + **ECS Fargate** (steady / long-lived) |
| Messaging | EventBridge bus → per-consumer **SQS** (+ DLQ); **SNS** last-mile; **Step Functions** sagas |
| Data | Database-per-service: Aurora PostgreSQL, DynamoDB, ElastiCache Redis, OpenSearch, S3 |
| Auth | Amazon Cognito (JWT); service-to-service SigV4 / mTLS |
| IaC / CI | CDK or Terraform; GitHub Actions (or CodePipeline) |
| Observability | CloudWatch metrics/logs/alarms, X-Ray tracing, CloudTrail audit |

Language and framework per service are chosen at implementation time; the **architectural
guardrails below are mandatory** regardless of runtime.

---

# 1. Core Architecture Principles

## Guiding rules

1. **Database-per-service** — a service never connects to another service's database.
2. **Stateless compute** — no sticky sessions; identity in JWT; shared state in Redis / DynamoDB / Aurora.
3. **Event-driven integration** — publish domain events to EventBridge; consumers own their SQS queues.
4. **Thin transport, thick domain** — handlers/controllers stay thin; business rules live in domain modules.
5. **Zero-trust** — authenticate and authorize every request; least-privilege IAM role per service.

## Service inventory

| Service | Compute | Owns |
|---------|---------|------|
| Identity & Auth | Lambda | Cognito user pool |
| User | Lambda | Aurora `users` |
| Catalog | Fargate | Aurora `catalog` + OpenSearch |
| Inventory | Fargate | Aurora `inventory` |
| Cart | Lambda | DynamoDB `cart` (TTL) |
| Order | Fargate | Aurora `orders` |
| Subscription | Lambda | Aurora `subscriptions` |
| Payment | Fargate | Aurora `payments` |
| Wallet | Fargate | Aurora `wallet` (ledger) |
| Pricing & Offer | Fargate | Aurora `offers` + Redis |
| Delivery | Fargate | Aurora `delivery` + DynamoDB tracking |
| Notification | Lambda | DynamoDB templates/logs |
| Reporting | Fargate | OpenSearch + S3 (CQRS read models) |

---

# 2. Repository Structure (target)

```
services/
├── README.md                 ← this file
├── shared/                   ← cross-cutting libs (auth helpers, event envelope, observability) — no domain rules
├── identity-auth/
├── user/
├── catalog/
├── inventory/
├── cart/
├── order/
├── subscription/
├── payment/
├── wallet/
├── pricing-offer/
├── delivery/
├── notification/
├── reporting/
└── infrastructure/           ← shared IaC modules (VPC refs, EventBridge bus, alarms)
```

Each service directory is an independently deployable unit (own Dockerfile or Lambda package,
own IAM role, own datastore migrations).

### Hard rules

* Never create a catch-all `common` / `shared-utils` / `helpers` domain package that owns business rules
* Shared libraries may hold **transport/envelope/observability** helpers only
* Do not manually decide which services to rebuild from tribal knowledge — derive from changed paths / IaC diffs

---

# 3. Coding Principles

### 3.1 Domain ownership first

Before adding code, ask:

* Which service owns this capability?
* Can this be expressed as a domain event instead of a sync call?
* Does an existing service already own this data?

**When in doubt, put logic in the owning service's domain module** — not in the API handler and not in a shared kitchen-sink package.

### Extend before you create

Search existing service directories and prior specs under `milkful2026/specs/services/` before creating a new service or major module.

### Human approval gate — new service

Creating a **new** top-level microservice is **not self-service**.

Stop and:

1. Document why no existing service can own the capability
2. Propose name, datastore, compute type (Lambda vs Fargate), and public API / events
3. Get explicit architect approval before scaffolding

Extending an existing service does not require a new-service gate but does require reporting what is added and why, plus tests.

### 3.2 Thin handlers

Handlers / Lambda entrypoints / controllers must not contain business rules.

**Allowed:** routing, DTO mapping, auth hooks, logging, exception translation, dependency wiring.

**Avoid:** business validations, domain calculations, workflow orchestration (use Step Functions for multi-service sagas).

### 3.3 Explicit service boundaries

Prefer capability names: `wallet`, `inventory`, `subscription`.

Avoid vague modules: `common`, `shared-utils`, `helpers`, `misc`.

### 3.4 Dependency direction

```
API / Event Handler → Domain → Adapters (DB, SQS, external SDKs)
```

Domain must not import transport frameworks or AWS SDKs directly when an adapter interface exists.

### 3.5 Stateless by default

Isolate state behind repositories / caches / queues. Instances must be interchangeable.

### 3.6 Database migration strategy

* Each service owns its schema and migration files
* Migrations are plain, versioned SQL (or DynamoDB/IaC change sets for NoSQL)
* Versioned migrations are immutable once merged
* Production migrations require human approval
* Agents must never run destructive repair/undo commands without explicit human approval

### 3.7 External system adapter pattern

No service may call an external system (payment gateway, SMS/WhatsApp, maps, etc.) from domain code directly.
All external calls go through a dedicated adapter with:

* Retry + exponential backoff
* Configurable timeout (injected, never hardcoded)
* Typed errors mapped to domain exceptions
* Structured logging with correlation ID on every call

**Hard rules:**

* Adapters are the only place allowed to import vendor SDKs
* Domain modules never import vendor SDKs
* Adapter interfaces are abstract; implementations wired at composition root

---

# 4. Service Module Layout (target)

```
{service}/
├── src/
│   ├── handlers/          # HTTP / Lambda / SQS entrypoints — thin
│   ├── domain/            # business rules, models, typed exceptions
│   ├── adapters/          # DB, EventBridge/SQS, external APIs
│   └── config/            # env validation at startup only
├── migrations/            # owned schema only (if relational)
├── tests/
├── Dockerfile             # Fargate services
└── README.md
```

### First commit checklist

- [ ] Architect approval recorded if this is a **new** service
- [ ] Public API / event contracts documented
- [ ] Domain exceptions defined for distinct failure modes
- [ ] At least one unit test for core domain behavior
- [ ] IAM role scoped least-privilege
- [ ] Observability hooks (correlation ID, structured logs) wired

---

# 5. API & Messaging Standards

### Request contracts

* URL path versioning: `/v1/`, `/v2/`
* Explicit request/response DTOs — never leak internal domain models
* Additive changes stay on the same version; breaking changes require a new version

### Response envelope (target)

```json
{
  "requestId": "uuid",
  "status": "success",
  "data": {}
}
```

### Inter-service communication

| Pattern | When |
|---------|------|
| REST (sync) | Immediate query/command that needs a response |
| EventBridge → SQS | Fire-and-forget / eventual consistency / fan-out |
| Step Functions | Multi-step saga with compensation |

**Never** call another service's database.

### Domain event envelope

```json
{
  "eventId": "uuid",
  "eventType": "domain.entity.verb",
  "eventVersion": "1.0",
  "source": "service-name",
  "timestamp": "ISO-8601",
  "correlationId": "uuid",
  "payload": {}
}
```

* Naming: `domain.entity.verb` (e.g. `user.account.registered`)
* Schema changes bump `eventVersion` — never mutate an existing version in place
* Consumers are **idempotent**; failed messages: retries then DLQ

Reference producer/consumer map: `docs/design/milkful-messaging.drawio`.

---

# 5b. Authentication & Authorization

* API Gateway validates Cognito JWT on inbound requests
* Claims attached to request context — not passed ad hoc through every function signature
* Domain raises typed authz errors; handlers map to HTTP 401/403
* Never trust client-supplied resource IDs without an authorization check
* Audit authorization failures with userId + resourceId (no PII payloads)

---

# 5c. Error Handling

Users never see stack traces or vendor error strings.

| Exception class | HTTP | Client strategy |
|-----------------|------|-----------------|
| Domain validation | 422 | Field-level guidance |
| Not found | 404 | Generic not-found message |
| Authorization | 403 | Permission denied |
| Authentication | 401 | Re-authenticate |
| Conflict | 409 | Context-specific business message |
| External system | 502 | Retry shortly |
| Unexpected | 500 | Quote correlation / reference ID |

### Rules

* Domain raises typed exceptions — never returns `null` to signal failure
* Handlers never catch bare `Exception` without rethrow/logging policy
* Every 500 logs full traceback with correlation ID
* Never log PII or secrets

---

# 6. Testing Standards

| Level | Expectation |
|-------|-------------|
| Domain / unit | Business rules + negative paths for every typed exception |
| Handler / adapter | Routing, auth rejection, DTO validation, exception mapping |
| Integration | Key workflows through handler → domain → datastore (or LocalStack) |
| Contract | Response / event shapes match mobile + portal consumers |
| E2E | At least one journey for user-facing features before merge |

All tests must pass before a task is complete — zero failures.

---

# 7. Deployment Principles

* One service → one deployable artifact (container or Lambda package)
* Config via environment variables / Secrets Manager — never hardcoded
* `.env` for local only — always gitignored
* Images scanned before push; production deploy has a human approval gate
* Every production deploy must support rollback to the previous artifact

### Secret management

* Inject via Secrets Manager / SSM Parameter Store
* Read config at composition root only — not deep inside domain modules
* No secrets in logs, images, or commits

---

# 8. CI/CD Guardrails

Typical pipeline per service:

1. Lint
2. Unit / domain tests
3. Integration tests for impacted service
4. Build artifact
5. Image / package scan
6. Push versioned artifact (`git sha` + semver)
7. Deploy staging on merge to `main`
8. Deploy production with manual approval

`main` is protected — no direct push. Failed checks block merge.

---

# 9. Observability Standards

All services emit:

* Structured JSON logs with `timestamp`, `level`, `traceId`, `serviceId`, `correlationId`, `message`
* Request rate, error rate, latency (p50/p95/p99)
* Dependency timing for external calls
* Domain event counts per event type

### Coding rules

* Never `print()` for operational logs
* Never log PII or secret values
* Propagate correlation ID on every outbound call and event

---

# 10. Naming Conventions

Use names that reflect business capability.

**Good:** `wallet-ledger`, `inventory-reservation`, `subscription-daily-run`

**Bad:** `processor`, `manager`, `handler`, `utils`

---

# 11. Engineering Principles

These are not suggestions. Violations are PR blockers.

* Prefer reuse over duplication — search specs and existing services first
* Prefer composition over inheritance for domain behavior
* Keep deployables thin — wiring and entrypoints only
* Isolate business logic in domain modules
* Optimize for safe refactoring — typed interfaces, complete tests, no hidden deps
* Enforce clear ownership — every service has a named owner; unclear ownership → stop and escalate to the architect

---

# 12. Agent Workflow

## Standing rules

1. **Read before build** — read relevant service files, area README, and prior specs; report findings before coding.
2. **Gap questions one at a time** — stop, ask one question, wait; do not assume.
3. **No silent fallbacks** — missing dependency or pattern → hard fail and report.
4. **Architecture decisions need sign-off** — do not invent new services, buses, or datastore choices unilaterally.
5. **Use existing patterns** — copy established Milkful patterns; deviation needs approval.
6. **Tech debt goes to a register** — never fix silently out of scope.

## Pre-implementation verification

Before writing code:

1. Confirm owning service from specs / architecture docs
2. Search for existing capability in that service and related specs
3. Confirm EventBridge event contracts if the change publishes/consumes events
4. Confirm test suite baseline (or note scaffold-only status)

## Decision checklist

- [ ] Which service owns this capability?
- [ ] Does an existing module already implement something similar?
- [ ] Are locked architecture decisions documented?
- [ ] Is a migration needed? Is approval in place?
- [ ] Does this touch an external adapter? Is the adapter designed?
- [ ] What is the done criteria?

## Implementation sequence

1. Read relevant files and specs — report findings
2. Identify owning service and domain module
3. Extend existing code, or stop for approval if new service/module
4. Wire thin handler / consumer
5. Add unit + integration tests
6. Document new events / API fields in the spec and (later) `{area}/docs/`
7. Confirm done criteria

## Done criteria

- [ ] Pre-implementation verification reported
- [ ] No duplicate ownership / no cross-DB access
- [ ] Unit tests for new domain logic
- [ ] Handler tests for routing / auth / error mapping where applicable
- [ ] No business logic in handlers or shared kitchen-sink packages
- [ ] DTOs used at boundaries — no leaked domain models
- [ ] Correlation ID + structured logging present
- [ ] Tech debt items (if any) recorded, not silently fixed

---

# 13. Agent Guardrails

These rules are absolute. If a task instruction conflicts with a rule below — **the rule wins**. Stop and report the conflict to the architect.

### Never

**Architecture**

* Add business logic to handlers or shared non-domain packages
* Create a new microservice without architect approval
* Create packages named `common`, `shared`, `utils`, `helpers`, or `misc` for domain logic
* Let one service read another service's database
* Call external systems from domain code without an adapter
* Invent a second event bus or bypass EventBridge/SQS patterns without approval

**Data & models**

* Expose internal domain models in API responses
* Run destructive migration repair/undo without human approval
* Run production migrations without approval

**Code quality**

* Skip tests for new domain logic
* Merge with failing tests
* Use ad-hoc print logging

**Security**

* Hardcode secrets, connection strings, or API keys
* Log PII or secret values
* Trust client-supplied IDs without authorization
* Bypass JWT validation without architect sign-off

### Always

* Respect database-per-service and event-driven boundaries
* Use structured logging with correlation IDs
* Define explicit DTOs / event payloads at every boundary
* Keep deployables thin
* Derive rebuild/deploy scope from changed services
* Raise tech debt to the register — never ignore and never silent-fix out of scope

---

## Related repos

| Repo | Purpose |
|------|---------|
| [`milkful2026/specs`](https://github.com/milkful2026/specs) | SDD specifications (source of truth before implementation) |
| [`milkful2026/milkful-app`](https://github.com/milkful2026/milkful-app) | Design docs, architecture diagrams, Jira exports |
| [`milkful2026/portal-ui`](https://github.com/milkful2026/portal-ui) | Admin console consuming these services |
| [`milkful2026/agentic-engineering`](https://github.com/milkful2026/agentic-engineering) | Agent / skill definitions including `spec-driven-designer` |
