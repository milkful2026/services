"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap — see product_repository.py); moto for SQS. No
real AWS credentials, DB, or network."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.product_repository import categories_table, create_schema, products_table


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CATALOG_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("CATALOG_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("CATALOG_STOCK_CHANGED_QUEUE_URL", "https://sqs.test/stock-changed")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def sqlite_engine():
    # StaticPool + check_same_thread=False — matches inventory's own
    # conftest fixture exactly, for the identical reason (FastAPI's
    # TestClient runs handlers in a worker thread pool; a plain in-memory
    # SQLite DB is thread-affine and per-connection without this).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    yield engine
    engine.dispose()


def seed_category(engine, **overrides) -> dict:
    row = {"id": "milk", "name": "Fresh Milk", "icon_name": "milk", "sort_order": 0}
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(categories_table.insert().values(**row))
    return row


@pytest.fixture
def sqs_queue():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("sqs", region_name="ap-south-1")
        queue = client.create_queue(QueueName="stock-changed")
        yield {"client": client, "queue_url": queue["QueueUrl"]}


def seed_product(engine, **overrides) -> dict:
    row = {
        "id": "cow-milk",
        "category_id": "milk",
        "name": "Cow Milk",
        "description": "Farm-fresh cow milk",
        "unit": "1L Bottle",
        "price_b2c": 68,
        "price_b2b": None,
        "image_url": None,
        "tag": "ORGANIC",
        "subscription_eligible": True,
        "is_veg": True,
        "is_organic": True,
        "stock_state": "IN_STOCK",
        "available_from": None,
        "last_stock_event_id": None,
    }
    row.update(overrides)
    with engine.begin() as conn:
        conn.execute(products_table.insert().values(**row))
    return row
