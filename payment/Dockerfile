# Build context must be services/ (the repo root), not payment/ — see
# docker-compose.yml's `context: .. / dockerfile: payment/Dockerfile`
# (same convention identity-auth/user already use), needed so this can
# also copy shared/ (services/README.md §2's cross-cutting libs, e.g.
# shared.adapters.retry, imported by this service's own adapters).
FROM python:3.11-slim

WORKDIR /app

COPY payment/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY payment/src/ ./
COPY shared/ ./shared/

EXPOSE 8007

CMD ["python", "main.py"]
