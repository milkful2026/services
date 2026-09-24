# Build context must be services/ (the repo root), not pricing-offer/ —
# see docker-compose.yml's `context: .. / dockerfile: pricing-offer/Dockerfile`
# (same convention wallet/order already use), needed so this can also
# copy shared/ (services/README.md §2's cross-cutting libs).
FROM python:3.11-slim

WORKDIR /app

COPY pricing-offer/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY pricing-offer/src/ ./
COPY shared/ ./shared/

EXPOSE 8005

CMD ["python", "main.py"]
