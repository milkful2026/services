"""Container/local entrypoint. No background consumer thread here (unlike
catalog/inventory) — this build has no event pipeline at all (see
README's "Scope" section), so there's nothing to run alongside the
FastAPI app. Every config value has a real default (config/env.py), so
this needs no `.env.local` loading either — `PRICING_CATALOG_BASE_URL`
is the only thing worth overriding, and docker-compose's own
`environment:` block does that for the containerized run.
"""

import uvicorn

from handlers.app import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8005)  # noqa: S104 — Fargate task, not exposed directly


if __name__ == "__main__":
    main()
