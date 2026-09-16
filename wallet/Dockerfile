# Build context must be services/ (the repo root), not wallet/ — see
# docker-compose.yml's `context: .. / dockerfile: wallet/Dockerfile`
# (same convention identity-auth/user already use), needed so this can
# also copy shared/ (services/README.md §2's cross-cutting libs, e.g.
# shared.events.load_schema, shared.adapters.retry, both already used by
# this service).
FROM python:3.11-slim

WORKDIR /app

COPY wallet/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY wallet/src/ ./
COPY shared/ ./shared/

EXPOSE 8006

CMD ["python", "main.py"]
