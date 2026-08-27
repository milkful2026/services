"""Shared pytest fixtures. moto for DynamoDB — no real AWS credentials or
network access required to run this suite (same convention as every
other service here, e.g. identity-auth/tests/conftest.py)."""

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CART_CATALOG_INTERNAL_BASE_URL", "http://catalog.test")
    monkeypatch.setenv("CART_USER_INTERNAL_BASE_URL", "http://user.test")
    monkeypatch.setenv("CART_PRICING_INTERNAL_BASE_URL", "http://pricing.test")
    monkeypatch.setenv("CART_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def aws(_env):
    with mock_aws():
        yield


@pytest.fixture
def cart_table(aws):
    ddb = boto3.resource("dynamodb", region_name="ap-south-1")
    table = ddb.create_table(
        TableName="cart",
        KeySchema=[
            {"AttributeName": "userId", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table
