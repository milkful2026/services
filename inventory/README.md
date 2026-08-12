# Inventory Service

Serviceability check API — determines whether a pincode/location is
within a deliverable zone, and what delivery slots are available there.
Implements [MA-1](https://milkfuldairyindia.atlassian.net/browse/MA-1) /
backend story [MA-95](https://milkfuldairyindia.atlassian.net/browse/MA-95),
per spec `specs/services/tasks/MA/MA-1/inventory-serviceability-api.md`.

## Endpoints

| Method | Path | Spec | Auth |
|--------|------|------|------|
| GET | `/v1/serviceability/check` | FR-1 | none — public, via API Gateway |
| GET | `/v1/internal/serviceability/check` | FR-2 | network-level only (VPC + security group) — see flagged decision below |

Query params: `pincode` (required, 6-digit Indian pincode), `lat`/`lng` (optional — refines match via point-in-polygon, preferred over pincode when both given).

## Architecture decisions flagged for review

1. **SQLite test double for `zone_repository`.** There's no moto-equivalent
   for Aurora, so `SqlAlchemyZoneRepository` is tested against an
   in-memory SQLite engine instead — a documented fidelity gap, not full
   Aurora emulation. `migrations/0001_serviceability_zones.sql` is the
   production-authoritative Postgres schema; the SQLAlchemy Core table in
   `zone_repository.py` deliberately uses portable column types (JSON, not
   native Postgres arrays or PostGIS geometry) so the same code path works
   against both.
2. **Point-in-polygon in Python (`shapely`), not PostGIS.** The repository
   only ever does `WHERE active = true` and hands raw zone rows to the
   domain layer — matching happens entirely in
   `domain/serviceability_service.py`, independent of the DB backend.
3. **Cache key is `svc:{pincode}` only** (no lat/lng component) — matches
   the spec exactly. A known simplification: two requests for the same
   pincode with different lat/lng can share a cached result until the
   15-min TTL expires, which only matters right at a polygon border.
4. **Internal endpoint auth is network-level, not literal mTLS/IAM.** Both
   routes are served by the same FastAPI app behind one internal ALB; the
   internal path is simply never registered with API Gateway, reachable
   only from within the VPC via the ALB's DNS name, security-group-
   restricted. Real mTLS would need ALB listener certificate
   configuration — flagged as a bigger CDK lift for architect follow-up,
   not built here.
5. **Single Fargate deployable** handles both the HTTP API and the
   `ZoneUpdated` SQS consumer (a background thread in the same process,
   `src/main.py`) — unlike Wallet's spec-mandated two-deployable split,
   nothing in this spec calls for separating them.
6. **No local Docker image build.** `ecs.ContainerImage.from_asset()`
   needs a running Docker daemon at `cdk synth`/deploy time — confirmed
   unavailable in this build's own environment (`docker --version` works,
   `docker ps` fails to reach the daemon). The CDK stack creates an empty
   ECR repository and references it via `from_ecr_repository`; the actual
   image is built and pushed by CI (services/README.md §8's pipeline),
   not by this stack.
7. **`DATABASE_URL` composition is not wired.** Aurora's
   `Credentials.from_generated_secret()` produces a Secrets Manager secret
   with separate JSON fields; CDK's ECS `Secret` injection maps one field
   to one env var, so it can't compose them into a single SQLAlchemy URL.
   The task definition injects `INVENTORY_DB_HOST`/`PORT`/`USERNAME`/
   `PASSWORD` discretely — composing the actual `INVENTORY_DATABASE_URL`
   is left as an application-startup or entrypoint-script concern for a
   human to wire, not invented here.
8. **Aurora Postgres Serverless v2**, not a provisioned cluster —
   cost-appropriate for a new, low-traffic service.

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate  # or .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
pytest                          # full suite: unit + integration + infra, no AWS/DB/Redis needed

cd infra
pip install -r requirements.txt
cdk synth                       # no AWS credentials, no Docker
```

## Testing approach

Fully offline — **no AWS credentials, no Docker, no real Postgres/Redis**:

- SQLite in-memory (`StaticPool`, `check_same_thread=False` — required
  because FastAPI's `TestClient` runs sync endpoints in a worker thread
  pool, and a plain in-memory SQLite DB is both thread-affine and
  per-connection) stands in for Aurora in repository/integration tests.
- `fakeredis` stands in for ElastiCache.
- `moto[sqs]` stands in for the `ZoneUpdated` queue.
- `httpx`/FastAPI `TestClient` exercises the real HTTP layer without a
  running server.
- `tests/infra/test_inventory_stack.py` uses `Template.from_stack`
  assertions against a real `cdk synth` — needs Node.js (CDK's JSII
  bridge) but no AWS account.

## What still needs a human

- `cdk bootstrap`/`cdk deploy` to a real AWS account.
- Building and pushing the container image to the ECR repo this stack
  creates (CI pipeline, per services/README.md §8) — the task definition
  references `:latest` in that repo, not a locally built image.
- Wiring real `DATABASE_URL` composition from the injected `INVENTORY_DB_*`
  secrets (flagged decision #7).
- Provisioning Aurora/ElastiCache for real and validating Fargate
  connectivity from inside the VPC.
- If the spec's literal "IAM/mTLS" internal-auth requirement is a hard
  requirement rather than acceptable as network-level-only, implementing
  real mTLS on the ALB listener (flagged decision #4).
- Confirming the (not-yet-built) Admin/zone-management service actually
  publishes `inventory.zone.updated` events matching this stack's
  EventBridge rule pattern.
- Measuring the NFR "p95 < 300ms (cache hit < 50ms)" against a deployed
  environment.

## Deferred / tech debt

- Waitlist (spec's flagged G2/Q1) — `waitlistAvailable` is always `false`
  until product adds the feature; not built here.
