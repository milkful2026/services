# Build context must be services/ (the repo root), not subscription/ —
# see docker-compose.yml's `context: .. / dockerfile: subscription/Dockerfile`
# (same convention wallet/payment use), needed so this can also copy
# shared/ (services/README.md §2's cross-cutting libs — shared.adapters.retry,
# shared.adapters.outbox_event_publisher, shared.handlers.auth, both
# already used by this service). Correct from day one — see the MA-25
# implementation plan §1 on why wallet/payment needed a follow-up fix for
# this instead.
FROM python:3.11-slim

WORKDIR /app

COPY subscription/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY subscription/src/ ./
COPY shared/ ./shared/

EXPOSE 8008

CMD ["python", "main.py"]
