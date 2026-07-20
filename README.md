# Milkful — Backend Services

AWS cloud-native backend for the Milkful dairy platform. **13 microservices** with
database-per-service, EventBridge event bus, API Gateway BFF, Cognito auth, hybrid
**Lambda + ECS Fargate** compute.

**Status:** scaffold only — no service code implemented yet.

| Service | Jira | Compute | Datastore |
|---------|------|---------|-----------|
| Identity & Auth | MA-92 | Lambda | Cognito |
| User | MA-93 | Lambda | Aurora `users` |
| Catalog | MA-94 | Fargate | Aurora + OpenSearch |
| Inventory | MA-95 | Fargate | Aurora `inventory` |
| Cart | MA-96 | Lambda | DynamoDB |
| Order | MA-97 | Fargate | Aurora `orders` |
| Subscription | MA-98 | Lambda | Aurora `subscriptions` |
| Payment | MA-99 | Fargate | Aurora `payments` |
| Wallet | MA-100 | Fargate | Aurora `wallet` |
| Pricing & Offer | MA-101 | Fargate | Aurora + Redis |
| Delivery | MA-102 | Fargate | Aurora + DynamoDB |
| Notification | MA-103 | Lambda | DynamoDB |
| Reporting | MA-104 | Fargate | OpenSearch + S3 |

**Jira Epic:** [MA-19 Backend Services](https://milkfuldairyindia.atlassian.net/browse/MA-19)
**Jira board:** [MA Backlog](https://milkfuldairyindia.atlassian.net/jira/software/projects/MA/boards/1/backlog)

## Related repos

| Repo | Purpose |
|------|---------|
| [`milkful2026/specs`](https://github.com/milkful2026/specs) | SDD specifications for every service (source of truth before implementation) |
| [`milkful2026/milkful-app`](https://github.com/milkful2026/milkful-app) | Design docs, architecture diagrams, SDD agent instructions |
| [`milkful2026/portal-ui`](https://github.com/milkful2026/portal-ui) | Admin console consuming these services |

## Architecture references

See `milkful2026/milkful-app`:
- `docs/design/milkful-well-architected.md`
- `docs/design/milkful-hld.drawio` / `milkful-lld.drawio`
- `docs/design/milkful-messaging.drawio`

## Implementation workflow

1. Specs are drafted per the SDD process in [`milkful2026/specs`](https://github.com/milkful2026/specs) (`services/tasks/MA/{STORY-KEY}/{SPEC-KEY}.md`).
2. Once a spec is `Spec: In Review` / approved, implementation begins here against that spec.
3. Each service should land as its own top-level directory once code starts (e.g. `identity-auth/`, `user/`, `catalog/`).
