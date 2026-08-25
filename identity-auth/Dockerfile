# Local-dev only image — runs run_local.py's plain-HTTP Lambda shim, not
# a real Lambda deployment (identity-auth ships as a Lambda zip/image
# built by its own CDK stack, never this Dockerfile). Build context is
# the repo root (services/) so this can also copy the two local-dev
# shim files run_local.py imports.
FROM python:3.11-slim

WORKDIR /app

COPY identity-auth/requirements.txt identity-auth/requirements-dev.txt ./identity-auth/
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r identity-auth/requirements-dev.txt

COPY identity-auth/ ./identity-auth/
COPY local-dev/_env_file.py local-dev/_lambda_local_server.py ./local-dev/

WORKDIR /app/identity-auth

EXPOSE 8001

CMD ["python", "run_local.py"]
