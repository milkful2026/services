"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap — see zone_repository.py); fakeredis for
ElastiCache; moto for SQS. No real AWS credentials, DB, or network."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.stock_repository import (
    create_schema as create_stock_schema,
    reservations_table,
    stock_batches_table,
    stock_table,
)
from adapters.zone_repository import create_schema, serviceability_zones_table


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INVENTORY_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("INVENTORY_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("INVENTORY_REDIS_HOST", "localhost")
    monkeypatch.setenv("INVENTORY_REDIS_PORT", "6379")
    monkeypatch.setenv("INVENTORY_ZONE_UPDATED_QUEUE_URL", "https://sqs.test/zone-updated")
    monkeypatch.setenv("INVENTORY_ORDER_CANCELLED_QUEUE_URL", "https://sqs.test/order-cancelled")
    monkeypatch.setenv("INVENTORY_CATALOG_UPDATED_QUEUE_URL", "https://sqs.test/catalog-updated")
    monkeypatch.setenv("INVENTORY_EVENT_BUS_NAME", "default")
    monkeypatch.setenv("INVENTORY_EVENT_SOURCE", "inventory")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def sqlite_engine():
    # StaticPool + check_same_thread=False: FastAPI's TestClient runs sync
    # endpoint functions in a worker thread pool, but a plain in-memory
    # SQLite DB is both thread-affine AND per-connection — without
    # StaticPool forcing every checkout to reuse the same connection, a
    # request handled on a different thread would silently see an empty
    # database instead of raising, since it'd be a distinct, never-seeded
    # in-memory DB.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


@pytest.fixture
def order_cancelled_queue():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("sqs", region_name="ap-south-1")
        queue = client.create_queue(QueueName="order-cancelled")
        yield {"client": client, "queue_url": queue["QueueUrl"]}


@pytest.fixture
def catalog_updated_queue():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("sqs", region_name="ap-south-1")
        queue = client.create_queue(QueueName="catalog-updated")
        yield {"client": client, "queue_url": queue["QueueUrl"]}


# --- MA-118/MA-119/MA-150 (stock/stock_batches/reservations/audit log) ---


@pytest.fixture
def stock_engine():
    # StaticPool + check_same_thread=False — see sqlite_engine's own
    # comment above; the concurrency test additionally needs this engine
    # usable safely from multiple threads, which StaticPool's single
    # shared connection provides for SQLite (though, per
    # stock_repository.py's own module docstring, SQLite's FOR UPDATE is
    # a documented no-op — real contention is only ever verified against
    # Postgres, see tests/concurrency/).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_stock_schema(engine)
    yield engine
    engine.dispose()


def seed_stock(engine, product_id: str = "cow-milk-1l", **overrides) -> dict:
    row = {
        "product_id": product_id,
        "on_hand": 0,
        "reserved": 0,
        "low_stock_threshold": 10,
    }
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(stock_table.insert().values(**row))
    return row


def seed_batch(engine, product_id: str, **overrides) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "product_id": product_id,
        "quantity": 10,
        "expiry_date": None,
        "available_from": None,
    }
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(stock_batches_table.insert().values(**row))
    return row


def seed_reservation(engine, product_id: str, order_ref: str, **overrides) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "product_id": product_id,
        "order_ref": order_ref,
        "quantity": 1,
        "status": "RESERVED",
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
    }
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(reservations_table.insert().values(**row))
    return row
