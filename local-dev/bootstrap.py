"""Provisions the AWS resources each service expects, against moto_server
(not real AWS) — the same role `cdk deploy` plays for a real account, but
via direct boto3 calls since moto_server has no CloudFormation engine.

Run once after `docker compose up -d`, before starting any service:

    python bootstrap.py

Idempotent-ish: safe to re-run against a moto_server that already has
these resources (existing-resource errors are swallowed), but moto_server
keeps everything in memory, so a container restart clears it and this
must be re-run.

Writes one `.env.local` per service (identity-auth/.env.local,
user/.env.local, inventory/.env.local) with the resource IDs it just
created — each service's config/env.py reads that file automatically
(see the `_LOCAL_ENV_FILE` pydantic-settings wiring). These files are
generated, not committed — see .gitignore.
"""

import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ENDPOINT_URL = "http://localhost:5000"
REGION = "us-east-1"
DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
INVENTORY_HTTP_URL = "http://localhost:8000"

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


def bootstrap_cognito() -> tuple[str, str]:
    client = boto3.client("cognito-idp", **_creds)

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


def bootstrap_sqs_and_eventbridge() -> str:
    sqs = boto3.client("sqs", **_creds)
    events = boto3.client("events", **_creds)

    try:
        dlq_url = sqs.create_queue(QueueName="zone-updated-dlq")["QueueUrl"]
    except ClientError as exc:
        if _ignore_already_exists(exc, "QueueAlreadyExists"):
            dlq_url = sqs.get_queue_url(QueueName="zone-updated-dlq")["QueueUrl"]
        else:
            raise
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]

    try:
        queue_url = sqs.create_queue(
            QueueName="zone-updated",
            Attributes={
                "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"})
            },
        )["QueueUrl"]
        print(f"[sqs] created queue {queue_url}")
    except ClientError as exc:
        if _ignore_already_exists(exc, "QueueAlreadyExists"):
            queue_url = sqs.get_queue_url(QueueName="zone-updated")["QueueUrl"]
            print(f"[sqs] queue zone-updated already exists, skipping")
        else:
            raise
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]

    # Mirrors inventory_stack.py's ZoneUpdatedRule (EventBridge -> SQS) so
    # PutEvents from the "inventory-admin" source actually lands on the
    # queue locally, the same path production uses — not just a queue
    # that only works if something calls SendMessage directly.
    try:
        events.put_rule(
            Name="ZoneUpdatedRule",
            EventPattern=json.dumps(
                {"source": ["inventory-admin"], "detail-type": ["inventory.zone.updated"]}
            ),
            State="ENABLED",
        )
        events.put_targets(
            Rule="ZoneUpdatedRule",
            Targets=[{"Id": "zone-updated-target", "Arn": queue_arn}],
        )
        print("[eventbridge] wired ZoneUpdatedRule -> zone-updated queue")
    except ClientError as exc:
        print(f"[eventbridge] rule wiring skipped (non-fatal for local dev): {exc}")

    # Local-dev-only debug queue: no real SMS provider exists locally, so
    # this is how a developer sees the plaintext OTP identity-auth
    # publishes to identity.otp.requested — see peek_otp.py. Nothing
    # like this exists in production; it's purely a local visibility aid.
    try:
        otp_debug_queue_url = sqs.create_queue(QueueName="otp-requested-debug")["QueueUrl"]
    except ClientError as exc:
        if _ignore_already_exists(exc, "QueueAlreadyExists"):
            otp_debug_queue_url = sqs.get_queue_url(QueueName="otp-requested-debug")["QueueUrl"]
        else:
            raise
    otp_debug_queue_arn = sqs.get_queue_attributes(
        QueueUrl=otp_debug_queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    try:
        events.put_rule(
            Name="OtpRequestedDebugRule",
            EventPattern=json.dumps(
                {"source": ["identity-auth"], "detail-type": ["identity.otp.requested"]}
            ),
            State="ENABLED",
        )
        events.put_targets(
            Rule="OtpRequestedDebugRule",
            Targets=[{"Id": "otp-debug-target", "Arn": otp_debug_queue_arn}],
        )
        print("[eventbridge] wired OtpRequestedDebugRule -> otp-requested-debug queue")
    except ClientError as exc:
        print(f"[eventbridge] otp debug rule wiring skipped (non-fatal for local dev): {exc}")

    return queue_url


def _write_env_file(service_dir: str, values: dict[str, str]) -> None:
    path = _SERVICES_DIR / service_dir / ".env.local"
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[env] wrote {path}")


def main() -> None:
    pool_id, client_id = bootstrap_cognito()
    table_name = bootstrap_dynamodb()
    queue_url = bootstrap_sqs_and_eventbridge()

    _write_env_file(
        "identity-auth",
        {
            "IDENTITY_AUTH_COGNITO_USER_POOL_ID": pool_id,
            "IDENTITY_AUTH_COGNITO_CLIENT_ID": client_id,
            "IDENTITY_AUTH_AWS_REGION": REGION,
            "IDENTITY_AUTH_AWS_ENDPOINT_URL": ENDPOINT_URL,
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
            "USER_AWS_ENDPOINT_URL": ENDPOINT_URL,
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
            "INVENTORY_AWS_ENDPOINT_URL": ENDPOINT_URL,
            "INVENTORY_REDIS_HOST": REDIS_HOST,
            "INVENTORY_REDIS_PORT": str(REDIS_PORT),
            "INVENTORY_REDIS_USE_TLS": "false",
            "INVENTORY_ZONE_UPDATED_QUEUE_URL": queue_url,
        },
    )
    print("\nDone. Next: python apply_migrations.py, then start each service.")


if __name__ == "__main__":
    main()
