"""Provisions the AWS resources each service expects, against moto_server
(not real AWS) — the same role `cdk deploy` plays for a real account, but
via direct boto3 calls since moto_server has no CloudFormation engine.

Run once after `docker compose up -d`, before starting any service:

    python bootstrap.py

Idempotent: safe to re-run against a moto_server that already has these
resources — every resource is looked up and reused if it already exists,
never silently recreated — but moto_server keeps everything in memory,
so a container restart clears it and this must be re-run.

Writes one `.env.local` per service (identity-auth/.env.local,
user/.env.local, inventory/.env.local) with the resource IDs it just
created. Each service's run_local.py-style entrypoint loads that file
into the real process environment at startup (see local-dev/_env_file.py)
before constructing any client, so both this service's own settings and
libraries that read env vars directly (e.g. boto3's native
AWS_ENDPOINT_URL support) see the values. These files are generated, not
committed — see .gitignore.
"""

import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Defaults target the host-side view (docker-compose's ports: mappings) —
# what you get running this script directly on your machine. Overridable
# so the same script also works from inside the one-shot "bootstrap"
# compose service, which sees moto/postgres/redis/inventory by their
# compose service name on the shared docker network, not localhost.
ENDPOINT_URL = os.environ.get("LOCAL_DEV_AWS_ENDPOINT_URL", "http://localhost:5000")
REGION = "us-east-1"
DB_HOST = os.environ.get("LOCAL_DEV_DB_HOST", "localhost")
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"
REDIS_HOST = os.environ.get("LOCAL_DEV_REDIS_HOST", "localhost")
REDIS_PORT = 6379
INVENTORY_HTTP_URL = os.environ.get("LOCAL_DEV_INVENTORY_HTTP_URL", "http://localhost:8000")
CATALOG_HTTP_URL = os.environ.get("LOCAL_DEV_CATALOG_HTTP_URL", "http://localhost:8003")
USER_HTTP_URL = os.environ.get("LOCAL_DEV_USER_HTTP_URL", "http://localhost:8002")
PRICING_HTTP_URL = os.environ.get("LOCAL_DEV_PRICING_HTTP_URL", "http://localhost:8005")

_SERVICES_DIR = Path(__file__).resolve().parent.parent

_creds = dict(
    aws_access_key_id="local",
    aws_secret_access_key="local",
    region_name=REGION,
    endpoint_url=ENDPOINT_URL,
)


def _ignore_already_exists(exc: ClientError, *codes: str) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in codes


def _get_or_create_queue(sqs, name: str, **create_kwargs) -> tuple[str, str]:
    """Returns (queue_url, queue_arn), reusing the queue if it already
    exists rather than erroring."""
    try:
        queue_url = sqs.create_queue(QueueName=name, **create_kwargs)["QueueUrl"]
        print(f"[sqs] created queue {name}")
    except ClientError as exc:
        if _ignore_already_exists(exc, "QueueAlreadyExists"):
            queue_url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            print(f"[sqs] queue {name} already exists, skipping")
        else:
            raise
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    return queue_url, queue_arn


def _wire_rule(events, rule_name: str, pattern: dict, target_id: str, target_arn: str) -> None:
    """put_rule/put_targets are both idempotent upserts — unlike queue
    creation there's no legitimate 'already exists' case to swallow, so
    any ClientError here is a genuine wiring failure and must propagate
    rather than being downgraded to a print that leaves the rest of the
    script reporting success."""
    events.put_rule(Name=rule_name, EventPattern=json.dumps(pattern), State="ENABLED")
    events.put_targets(Rule=rule_name, Targets=[{"Id": target_id, "Arn": target_arn}])
    print(f"[eventbridge] wired {rule_name} -> {target_id}")


def bootstrap_cognito() -> tuple[str, str]:
    # Real (and moto) Cognito doesn't error on a duplicate PoolName, so
    # unlike the DynamoDB/SQS bootstrap functions this can't rely on
    # catching an "already exists" ClientError — it has to look first.
    # Without this, re-running bootstrap.py against a still-running
    # moto_server silently creates a second pool + client every time,
    # orphaning any state (e.g. registered users) built against the
    # first one.
    client = boto3.client("cognito-idp", **_creds)

    pools = client.list_user_pools(MaxResults=60)["UserPools"]
    existing_pool = next((p for p in pools if p["Name"] == "milkful-local"), None)
    if existing_pool is not None:
        pool_id = existing_pool["Id"]
        print(f"[cognito] user pool milkful-local already exists ({pool_id}), skipping")
    else:
        try:
            pool = client.create_user_pool(
                PoolName="milkful-local",
                UsernameAttributes=["phone_number"],
                AutoVerifiedAttributes=["phone_number", "email"],
                Schema=[
                    {"Name": "phone_number", "AttributeDataType": "String", "Mutable": True},
                    {"Name": "email", "AttributeDataType": "String", "Mutable": True},
                    {"Name": "name", "AttributeDataType": "String", "Mutable": True},
                    {"Name": "google_sub", "AttributeDataType": "String", "Mutable": True},
                    {"Name": "apple_sub", "AttributeDataType": "String", "Mutable": True},
                    {"Name": "default_pincode", "AttributeDataType": "String", "Mutable": True},
                ],
            )
            pool_id = pool["UserPool"]["Id"]
            print(f"[cognito] created user pool {pool_id}")
        except ClientError as exc:
            print(f"[cognito] create_user_pool failed: {exc}", file=sys.stderr)
            raise

    pool_clients = client.list_user_pool_clients(
        UserPoolId=pool_id, MaxResults=60
    )["UserPoolClients"]
    existing_client = next(
        (c for c in pool_clients if c["ClientName"] == "milkful-local-client"),
        None,
    )
    if existing_client is not None:
        client_id = existing_client["ClientId"]
        print(f"[cognito] app client milkful-local-client already exists ({client_id}), skipping")
    else:
        client_resp = client.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName="milkful-local-client",
            GenerateSecret=False,
            ExplicitAuthFlows=[
                "ALLOW_ADMIN_USER_PASSWORD_AUTH",
                "ALLOW_USER_PASSWORD_AUTH",
                "ALLOW_REFRESH_TOKEN_AUTH",
            ],
        )
        client_id = client_resp["UserPoolClient"]["ClientId"]
        print(f"[cognito] created app client {client_id}")
    return pool_id, client_id


def bootstrap_dynamodb() -> str:
    client = boto3.client("dynamodb", **_creds)
    table_name = "otp_requests"
    try:
        client.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "requestId", "AttributeType": "S"},
                {"AttributeName": "mobile", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "mobile-index",
                    "KeySchema": [{"AttributeName": "mobile", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        print(f"[dynamodb] created table {table_name}")
    except ClientError as exc:
        if _ignore_already_exists(exc, "ResourceInUseException"):
            print(f"[dynamodb] table {table_name} already exists, skipping")
        else:
            raise

    try:
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
    except ClientError as exc:
        # moto's TTL support is best-effort; not fatal for local dev since
        # nothing here depends on actual expiry — OTP flows are exercised
        # manually, well within the real 5-minute TTL window.
        print(f"[dynamodb] update_time_to_live skipped: {exc}")

    return table_name


def bootstrap_cart_table() -> str:
    # Same shape as bootstrap_dynamodb()'s otp_requests table — single
    # table, TTL enabled, no GSI needed since cart_repository.py's every
    # access pattern is PK-only (Query by userId) or a full Scan
    # (the outbox drain), never a secondary-attribute lookup.
    client = boto3.client("dynamodb", **_creds)
    table_name = "cart"
    try:
        client.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
        )
        print(f"[dynamodb] created table {table_name}")
    except ClientError as exc:
        if _ignore_already_exists(exc, "ResourceInUseException"):
            print(f"[dynamodb] table {table_name} already exists, skipping")
        else:
            raise

    try:
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
        )
    except ClientError as exc:
        # Same moto caveat as bootstrap_dynamodb()'s own otp_requests
        # table — best-effort, not fatal for local dev.
        print(f"[dynamodb] update_time_to_live skipped: {exc}")

    return table_name


def bootstrap_sqs_and_eventbridge() -> str:
    sqs = boto3.client("sqs", **_creds)
    events = boto3.client("events", **_creds)

    _dlq_url, dlq_arn = _get_or_create_queue(sqs, "zone-updated-dlq")

    queue_url, queue_arn = _get_or_create_queue(
        sqs,
        "zone-updated",
        Attributes={
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"})
        },
    )

    # Mirrors inventory_stack.py's ZoneUpdatedRule (EventBridge -> SQS) so
    # PutEvents from the "inventory-admin" source actually lands on the
    # queue locally, the same path production uses — not just a queue
    # that only works if something calls SendMessage directly.
    _wire_rule(
        events,
        "ZoneUpdatedRule",
        {"source": ["inventory-admin"], "detail-type": ["inventory.zone.updated"]},
        "zone-updated-target",
        queue_arn,
    )

    # Local-dev-only debug queue: no real SMS provider exists locally, so
    # this is how a developer sees the plaintext OTP identity-auth
    # publishes to identity.otp.requested — see peek_otp.py. Nothing
    # like this exists in production; it's purely a local visibility aid.
    _otp_debug_queue_url, otp_debug_queue_arn = _get_or_create_queue(sqs, "otp-requested-debug")
    _wire_rule(
        events,
        "OtpRequestedDebugRule",
        {"source": ["identity-auth"], "detail-type": ["identity.otp.requested"]},
        "otp-debug-target",
        otp_debug_queue_arn,
    )

    return queue_url


def bootstrap_stock_changed_queue() -> str:
    """MA-116 FR-5 / MA-118 FR-6's agreed `StockChanged` contract — wired
    the same way as `zone-updated` above (EventBridge -> per-consumer SQS
    + DLQ). No real producer exists yet (Inventory's reserve/commit/
    release, MA-118, is spec'd but not implemented) — this provisions
    Catalog's consumer side of the contract now so it's provable via a
    directly-published test event ahead of that landing."""
    sqs = boto3.client("sqs", **_creds)
    events = boto3.client("events", **_creds)

    _dlq_url, dlq_arn = _get_or_create_queue(sqs, "stock-changed-dlq")
    queue_url, queue_arn = _get_or_create_queue(
        sqs,
        "stock-changed",
        Attributes={
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"})
        },
    )
    _wire_rule(
        events,
        "StockChangedRule",
        {"source": ["inventory"], "detail-type": ["inventory.stock.changed"]},
        "stock-changed-target",
        queue_arn,
    )
    return queue_url


def _write_env_file(service_dir: str, values: dict[str, str]) -> None:
    # Overridable so the one-shot "bootstrap" compose service can write
    # each service's .env.local into a shared docker volume (mounted at
    # e.g. /shared/<service>) instead of the host path — the app
    # containers mount that same volume and read .env.local from there,
    # rather than sharing a host bind-mount (which would race the file's
    # first-ever creation against the app container's own mount setup).
    output_root = Path(os.environ.get("LOCAL_DEV_ENV_OUTPUT_ROOT", _SERVICES_DIR))
    path = output_root / service_dir / ".env.local"
    path.parent.mkdir(parents=True, exist_ok=True)
    # boto3's default credential chain (env vars / ~/.aws/credentials /
    # IAM role) needs *something* present or every client construction
    # raises NoCredentialsError before a single request reaches moto —
    # which doesn't check these values, so any non-empty string works.
    # Every service here talks to moto only, so these are written
    # unconditionally rather than relying on the developer's own shell
    # already having AWS creds exported (true on the machine this was
    # built on, but not guaranteed elsewhere — and never true inside a
    # freshly built container).
    all_values = {"AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local", **values}
    lines = [f"{key}={value}" for key, value in all_values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[env] wrote {path}")


def main() -> None:
    pool_id, client_id = bootstrap_cognito()
    table_name = bootstrap_dynamodb()
    cart_table_name = bootstrap_cart_table()
    queue_url = bootstrap_sqs_and_eventbridge()
    stock_changed_queue_url = bootstrap_stock_changed_queue()

    # AWS_ENDPOINT_URL (unprefixed): the standard env var name botocore
    # itself reads natively — written once per service's .env.local so
    # each service's run_local.py-style entrypoint can load it into the
    # real process environment before boto3 constructs any client. No
    # per-service Settings field or adapter parameter needed for it.
    _write_env_file(
        "identity-auth",
        {
            "IDENTITY_AUTH_COGNITO_USER_POOL_ID": pool_id,
            "IDENTITY_AUTH_COGNITO_CLIENT_ID": client_id,
            "IDENTITY_AUTH_AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": ENDPOINT_URL,
            "IDENTITY_AUTH_OTP_REQUESTS_TABLE_NAME": table_name,
            "IDENTITY_AUTH_REDIS_HOST": REDIS_HOST,
            "IDENTITY_AUTH_REDIS_PORT": str(REDIS_PORT),
            "IDENTITY_AUTH_REDIS_USE_TLS": "false",
            "IDENTITY_AUTH_EVENT_BUS_NAME": "default",
        },
    )
    _write_env_file(
        "user",
        {
            "USER_DATABASE_URL": (
                f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/milkful_user"
            ),
            "USER_AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": ENDPOINT_URL,
            "USER_COGNITO_USER_POOL_ID": pool_id,
            "USER_INVENTORY_INTERNAL_BASE_URL": INVENTORY_HTTP_URL,
            "USER_EVENT_BUS_NAME": "default",
        },
    )
    _write_env_file(
        "inventory",
        {
            "INVENTORY_DATABASE_URL": (
                f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/milkful_inventory"
            ),
            "INVENTORY_AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": ENDPOINT_URL,
            "INVENTORY_REDIS_HOST": REDIS_HOST,
            "INVENTORY_REDIS_PORT": str(REDIS_PORT),
            "INVENTORY_REDIS_USE_TLS": "false",
            "INVENTORY_ZONE_UPDATED_QUEUE_URL": queue_url,
            # Local dev only — see handlers/app.py's comment. The Flutter
            # web build runs on a different localhost port than this
            # service, so the browser blocks every call as cross-origin
            # without this. Read directly from os.environ there (not
            # through config.env.Settings) to avoid forcing eager
            # Settings validation at module-import time.
            "INVENTORY_CORS_ALLOW_ALL": "true",
        },
    )
    _write_env_file(
        "catalog",
        {
            "CATALOG_DATABASE_URL": (
                f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/milkful_catalog"
            ),
            "CATALOG_AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": ENDPOINT_URL,
            "CATALOG_STOCK_CHANGED_QUEUE_URL": stock_changed_queue_url,
            # Local dev only — see inventory's identical entry above for
            # why (Flutter web's browser-origin CORS block).
            "CATALOG_CORS_ALLOW_ALL": "true",
        },
    )
    _write_env_file(
        "cart",
        {
            # Every key here is prefixed CART_CART_*/CART_* per
            # config.env.Settings' own SettingsConfigDict(env_prefix=
            # "CART_") — pydantic-settings prepends the prefix to the
            # field name verbatim, it doesn't replace a leading "cart_"
            # already in it (matches e.g. identity-auth's own
            # IDENTITY_AUTH_OTP_REQUESTS_TABLE_NAME for
            # otp_requests_table_name).
            "CART_AWS_REGION": REGION,
            "AWS_ENDPOINT_URL": ENDPOINT_URL,
            "CART_CART_TABLE_NAME": cart_table_name,
            "CART_EVENT_BUS_NAME": "default",
            "CART_CATALOG_INTERNAL_BASE_URL": CATALOG_HTTP_URL,
            "CART_USER_INTERNAL_BASE_URL": USER_HTTP_URL,
            "CART_PRICING_INTERNAL_BASE_URL": PRICING_HTTP_URL,
            # Left unset — matches config.env.Settings' own "" default:
            # MA-100 (Wallet Service) doesn't exist, so there's no real
            # URL to point at (HttpWalletClient never uses this value
            # today regardless — see its own module docstring).
        },
    )
    print("\nDone. Next: python apply_migrations.py, then start each service.")


if __name__ == "__main__":
    main()
