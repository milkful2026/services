"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap — see zone_repository.py); fakeredis for
ElastiCache; moto for SQS. No real AWS credentials, DB, or network."""

import pytest
from sqlalchemy import create_engine

from adapters.zone_repository import create_schema, serviceability_zones_table


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INVENTORY_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("INVENTORY_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("INVENTORY_REDIS_HOST", "localhost")
    monkeypatch.setenv("INVENTORY_REDIS_PORT", "6379")
    monkeypatch.setenv("INVENTORY_ZONE_UPDATED_QUEUE_URL", "https://sqs.test/zone-updated")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def sqlite_engine():
    # A fresh in-memory DB per test — StaticPool would share one
    # connection across threads, not needed here since tests are
    # single-threaded and each test gets its own engine instance.
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    yield engine
    engine.dispose()


def seed_zone(engine, **overrides) -> dict:
    row = {
        "id": "blr-central",
        "name": "Bangalore Central",
        "active": True,
        "pincode_prefixes": ["5600"],
        "polygon": None,
        "slot_config": [{"id": "morning-6-8", "label": "Morning 6-8 AM"}],
    }
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(serviceability_zones_table.insert().values(**row))
    return row


@pytest.fixture
def fake_redis():
    import fakeredis

    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def sqs_queue():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("sqs", region_name="ap-south-1")
        queue = client.create_queue(QueueName="zone-updated")
        yield {"client": client, "queue_url": queue["QueueUrl"]}
