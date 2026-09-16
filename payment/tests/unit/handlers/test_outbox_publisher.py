"""handlers/outbox_publisher.run_once() — publishes unpublished rows and
emits `outbox.publish_lag_seconds` reflecting the oldest unpublished
row's age (MA-126 §5)."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import create_engine

from adapters.payment_repository import SqlAlchemyPaymentRepository, create_schema, payments_table
from handlers import outbox_publisher


@pytest.fixture
def engine(monkeypatch, tmp_path):
    db_path = tmp_path / "payment.db"
    monkeypatch.setenv("PAYMENT_DATABASE_URL", f"sqlite:///{db_path}")
    eng = create_engine(f"sqlite:///{db_path}")
    create_schema(eng)
    yield eng
    eng.dispose()


def _seed_payment_and_outbox_row(engine, created_at):
    with engine.begin() as conn:
        conn.execute(
            payments_table.insert().values(
                id="pay_1", user_id="user-1", purpose="WALLET_RECHARGE", amount_paise=1000,
                currency="INR", status="CONFIRMED", idempotency_key="k1", correlation_id="c1",
            )
        )
    repo = SqlAlchemyPaymentRepository(engine)
    repo.enqueue_outbox("pay_1", "PaymentConfirmed", {"paymentId": "pay_1"})
    with engine.begin() as conn:
        from adapters.payment_repository import outbox_table

        conn.execute(outbox_table.update().values(created_at=created_at))


@contextmanager
def _mock_bus():
    with mock_aws():
        boto3.client("events", region_name="ap-south-1").create_event_bus(Name="milkful-events")
        yield


def test_publish_lag_metric_reflects_oldest_row_age(engine, caplog):
    old_ts = datetime.now(UTC) - timedelta(seconds=42)
    _seed_payment_and_outbox_row(engine, old_ts)

    with _mock_bus(), caplog.at_level(logging.INFO, logger="payment.metrics"):
        published = outbox_publisher.run_once()

    assert published == 1
    lag_records = [
        r for r in caplog.records if getattr(r, "metric", None) == "outbox.publish_lag_seconds"
    ]
    assert len(lag_records) == 1
    assert lag_records[0].value >= 42


def test_no_unpublished_rows_emits_no_lag_metric(engine, caplog):
    with _mock_bus(), caplog.at_level(logging.INFO, logger="payment.metrics"):
        published = outbox_publisher.run_once()
    assert published == 0
    assert not any(
        getattr(r, "metric", None) == "outbox.publish_lag_seconds" for r in caplog.records
    )


def test_row_marked_published_after_success(engine):
    _seed_payment_and_outbox_row(engine, datetime.now(UTC))
    repo = SqlAlchemyPaymentRepository(engine)
    with _mock_bus():
        outbox_publisher.run_once()
    assert repo.fetch_unpublished() == []
