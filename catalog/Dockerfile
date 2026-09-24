# Build context must be services/ (the repo root), not catalog/ — see
# docker-compose.yml's `context: .. / dockerfile: catalog/Dockerfile`
# (same convention wallet/order already use), needed so this can also
# copy shared/ (services/README.md §2's cross-cutting libs).
FROM python:3.11-slim

WORKDIR /app

COPY catalog/requirements.txt .
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

COPY catalog/src/ ./
COPY shared/ ./shared/

EXPOSE 8003

CMD ["python", "main.py"]
