"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap — see user_repository.py); moto for Cognito/
EventBridge; `responses` for the Inventory HTTP call. No real AWS
credentials, DB, or network."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.user_repository import create_schema


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("USER_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("USER_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("USER_COGNITO_USER_POOL_ID", "ap-south-1_testpool")
    monkeypatch.setenv("USER_INVENTORY_INTERNAL_BASE_URL", "http://inventory.internal.test")
    monkeypatch.setenv("USER_EVENT_BUS_NAME", "default")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def sqlite_engine():
    # StaticPool + check_same_thread=False — see MA-95's zone_repository
    # for why this is required for any test exercising handlers (FastAPI/
    # Lambda-shaped sync callables run via a thread pool in some test
    # clients; kept consistent here even though Lambda handlers in these
    # tests are called directly, not through a threaded test client).
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def aws():
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture
def cognito_user_pool(aws):
    import os

    import boto3

    client = boto3.client("cognito-idp", region_name="ap-south-1")
    pool = client.create_user_pool(
        PoolName="milkful-test-pool",
        UsernameAttributes=["phone_number"],
        Schema=[{"Name": "phone_number", "AttributeDataType": "String", "Mutable": True}],
    )
    pool_id = pool["UserPool"]["Id"]
    os.environ["USER_COGNITO_USER_POOL_ID"] = pool_id
    return {"client": client, "pool_id": pool_id}


@pytest.fixture
def event_bus(aws):
    import boto3

    return boto3.client("events", region_name="ap-south-1")
