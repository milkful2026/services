# Local-dev only image — runs run_local.py's plain-HTTP Lambda shim, not
# a real Lambda deployment (user ships as a Lambda zip/image built by
# its own CDK stack, never this Dockerfile). Build context is the repo
# root (services/) so this can also copy the two local-dev shim files
# run_local.py imports.
FROM python:3.11-slim

WORKDIR /app

COPY user/requirements.txt user/requirements-dev.txt ./user/
RUN pip install --no-cache-dir -r user/requirements-dev.txt

COPY user/ ./user/
COPY local-dev/_env_file.py local-dev/_lambda_local_server.py ./local-dev/

WORKDIR /app/user

EXPOSE 8002

CMD ["python", "run_local.py"]
