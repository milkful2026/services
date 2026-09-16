"""Shared pytest fixtures.

Everything here is a local test double — moto for AWS services, fakeredis
for Redis, `responses` for HTTP (JWKS endpoints). No real AWS credentials
or network access are required to run this suite.
"""

import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Populate every required env var so config.env.Settings() succeeds."""
    monkeypatch.setenv("IDENTITY_AUTH_COGNITO_USER_POOL_ID", "ap-south-1_testpool")
    monkeypatch.setenv("IDENTITY_AUTH_COGNITO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("IDENTITY_AUTH_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("IDENTITY_AUTH_OTP_REQUESTS_TABLE_NAME", "otp_requests")
    monkeypatch.setenv("IDENTITY_AUTH_REDIS_HOST", "localhost")
    monkeypatch.setenv("IDENTITY_AUTH_REDIS_PORT", "6379")
    monkeypatch.setenv("IDENTITY_AUTH_EVENT_BUS_NAME", "default")
    monkeypatch.setenv("IDENTITY_AUTH_GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("IDENTITY_AUTH_APPLE_CLIENT_ID", "test-apple-client-id")
    monkeypatch.setenv("IDENTITY_AUTH_ADMIN_COGNITO_USER_POOL_ID", "ap-south-1_admintestpool")
    monkeypatch.setenv("IDENTITY_AUTH_ADMIN_COGNITO_CLIENT_ID", "test-admin-client-id")
    monkeypatch.setenv("IDENTITY_AUTH_ADMIN_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def aws(_env):
    """Active moto mock covering dynamodb, cognito-idp, and events."""
    with mock_aws():
        yield


@pytest.fixture
def otp_table(aws):
    ddb = boto3.resource("dynamodb", region_name="ap-south-1")
    table = ddb.create_table(
        TableName="otp_requests",
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "requestId", "AttributeType": "S"},
            {"AttributeName": "mobile", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "mobile-index",
                "KeySchema": [{"AttributeName": "mobile", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    table.wait_until_exists()
    return table


@pytest.fixture
def cognito_user_pool(aws):
    client = boto3.client("cognito-idp", region_name="ap-south-1")
    pool = client.create_user_pool(
        PoolName="milkful-test-pool",
        UsernameAttributes=["phone_number", "email"],
        AutoVerifiedAttributes=["phone_number", "email"],
        Schema=[
            {"Name": "phone_number", "AttributeDataType": "String", "Mutable": True},
            {"Name": "email", "AttributeDataType": "String", "Mutable": True},
            {
                "Name": "google_sub",
                "AttributeDataType": "String",
                "Mutable": True,
                "DeveloperOnlyAttribute": False,
            },
            {
                "Name": "apple_sub",
                "AttributeDataType": "String",
                "Mutable": True,
                "DeveloperOnlyAttribute": False,
            },
        ],
    )
    pool_id = pool["UserPool"]["Id"]
    app_client = client.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="milkful-test-client",
        ExplicitAuthFlows=[
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
            "ALLOW_USER_PASSWORD_AUTH",
        ],
    )
    os.environ["IDENTITY_AUTH_COGNITO_USER_POOL_ID"] = pool_id
    os.environ["IDENTITY_AUTH_COGNITO_CLIENT_ID"] = app_client["UserPoolClient"]["ClientId"]
    return {"client": client, "pool_id": pool_id, "app_client_id": app_client["UserPoolClient"]["ClientId"]}


@pytest.fixture
def fake_redis():
    import fakeredis

    return fakeredis.FakeStrictRedis()


@pytest.fixture
def admin_sqlite_engine():
    """SQLite in-memory stands in for this service's own, new Aurora
    instance backing `admin_user` (MA-129) — same documented fidelity
    gap as services/user's user_repository.py test double."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from adapters.admin_user_repository import create_schema

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def admin_cognito_pool(aws):
    """A second, separate Cognito User Pool standing in for the Admin
    Pool (MA-129) — distinct from `cognito_user_pool`'s consumer OTP
    pool fixture above, per the spec's "separate pool" decision."""
    client = boto3.client("cognito-idp", region_name="ap-south-1")
    pool = client.create_user_pool(
        PoolName="milkful-admin-test-pool",
        UsernameAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        Schema=[{"Name": "email", "AttributeDataType": "String", "Mutable": True}],
    )
    pool_id = pool["UserPool"]["Id"]
    app_client = client.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="milkful-admin-test-client",
        ExplicitAuthFlows=[
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
            "ALLOW_USER_PASSWORD_AUTH",
        ],
    )
    for group in ("Ops", "Finance", "Support", "Marketing", "SuperAdmin"):
        client.create_group(GroupName=group, UserPoolId=pool_id)
    os.environ["IDENTITY_AUTH_ADMIN_COGNITO_USER_POOL_ID"] = pool_id
    os.environ["IDENTITY_AUTH_ADMIN_COGNITO_CLIENT_ID"] = app_client["UserPoolClient"]["ClientId"]
    return {
        "client": client,
        "pool_id": pool_id,
        "app_client_id": app_client["UserPoolClient"]["ClientId"],
    }


@pytest.fixture
def event_bus(aws):
    client = boto3.client("events", region_name="ap-south-1")
    return client
