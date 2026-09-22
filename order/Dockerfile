# Build context must be services/ (the repo root), not order/ — see
# docker-compose.yml's `context: .. / dockerfile: order/Dockerfile`
# (same convention wallet/payment/subscription use), needed so this can
# also copy shared/ (services/README.md §2's cross-cutting libs —
# shared.adapters.retry, shared.adapters.outbox_event_publisher,
# shared.handlers.auth). Correct from day one.
FROM python:3.11-slim

WORKDIR /app

COPY order/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY order/src/ ./
COPY shared/ ./shared/

EXPOSE 8009

CMD ["python", "main.py"]
