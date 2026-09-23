# Local-dev only image — runs run_local.py's plain-HTTP Lambda shim, not
# a real Lambda deployment (cart ships as a Lambda zip/image built by its
# own CDK stack, never this Dockerfile). Build context is the repo root
# (services/) so this can also copy the two local-dev shim files
# run_local.py imports — same convention identity-auth/user already use.
FROM python:3.11-slim

WORKDIR /app

COPY cart/requirements.txt cart/requirements-dev.txt ./cart/
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r cart/requirements-dev.txt

COPY cart/ ./cart/
COPY local-dev/_env_file.py local-dev/_lambda_local_server.py ./local-dev/

WORKDIR /app/cart

EXPOSE 8004

CMD ["python", "run_local.py"]
