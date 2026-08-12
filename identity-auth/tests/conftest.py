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
    pool = client.create_user_pool(PoolName="milkful-test-pool")
    pool_id = pool["UserPool"]["Id"]
    app_client = client.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="milkful-test-client",
        ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )
    os.environ["IDENTITY_AUTH_COGNITO_USER_POOL_ID"] = pool_id
    os.environ["IDENTITY_AUTH_COGNITO_CLIENT_ID"] = app_client["UserPoolClient"]["ClientId"]
    return {"client": client, "pool_id": pool_id, "app_client_id": app_client["UserPoolClient"]["ClientId"]}


@pytest.fixture
def fake_redis():
    import fakeredis

    return fakeredis.FakeStrictRedis()


@pytest.fixture
def event_bus(aws):
    client = boto3.client("events", region_name="ap-south-1")
    return client
