"""MA-118 FR-8. See adapters/catalog_updated_consumer.py's module
docstring for why this consumer exists despite not being its own listed
impl-plan step, and why — unlike OrderCancelled — Catalog has no real
producer capability at all today, not just "not built yet"."""

import json
import logging

import pytest

from adapters.catalog_updated_consumer import CatalogUpdatedConsumer
from domain.exceptions import ServiceUnavailableError


class FakeStockService:
    def __init__(self):
        self.provisioned: list[str] = []
        self.raises: Exception | None = None

    def provision_product(self, product_id):
        if self.raises is not None:
            raise self.raises
        self.provisioned.append(product_id)
        return True


def _send_catalog_updated(queue, product_id="cow-milk-1l", correlation_id="corr-1") -> None:
    body = {"correlationId": correlation_id, "payload": {"productId": product_id}}
    queue["client"].send_message(QueueUrl=queue["queue_url"], MessageBody=json.dumps(body))


@pytest.fixture
def stock_service():
    return FakeStockService()


@pytest.fixture
def consumer(catalog_updated_queue, stock_service):
    return CatalogUpdatedConsumer(
        queue_url=catalog_updated_queue["queue_url"],
        stock_service=stock_service,
        region_name="ap-south-1",
    )


def test_poll_once_provisions_the_product(catalog_updated_queue, consumer, stock_service):
    _send_catalog_updated(catalog_updated_queue, product_id="cow-milk-1l")

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.provisioned == ["cow-milk-1l"]


def test_poll_once_deletes_message_after_processing(catalog_updated_queue, consumer):
    _send_catalog_updated(catalog_updated_queue)
    consumer.poll_once(wait_time_seconds=0)

    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_poll_once_with_no_messages_returns_zero(consumer):
    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_malformed_message_is_left_in_queue_not_deleted(
    catalog_updated_queue, consumer, stock_service
):
    catalog_updated_queue["client"].send_message(
        QueueUrl=catalog_updated_queue["queue_url"], MessageBody="not valid json"
    )

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.provisioned == []


def test_missing_product_id_does_not_raise(catalog_updated_queue, consumer, stock_service):
    catalog_updated_queue["client"].send_message(
        QueueUrl=catalog_updated_queue["queue_url"], MessageBody=json.dumps({"payload": {}})
    )

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert stock_service.provisioned == []


def test_redelivered_event_for_an_already_provisioned_product_is_a_noop(
    catalog_updated_queue, consumer, stock_service
):
    # FR-8: provision_product() itself is the idempotent
    # INSERT...ON CONFLICT DO NOTHING — this consumer just needs to not
    # error on a redelivery, which it doesn't (provision_product never
    # raises for an already-provisioned product).
    _send_catalog_updated(catalog_updated_queue, product_id="cow-milk-1l")

    consumer.poll_once(wait_time_seconds=0)
    _send_catalog_updated(catalog_updated_queue, product_id="cow-milk-1l")
    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.provisioned == ["cow-milk-1l", "cow-milk-1l"]


def test_a_transient_failure_is_left_in_queue_not_raised(
    catalog_updated_queue, consumer, stock_service
):
    stock_service.raises = ServiceUnavailableError("db blip")
    _send_catalog_updated(catalog_updated_queue)

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert stock_service.provisioned == []


def test_process_message_failure_logs_use_the_event_own_correlation_id(
    catalog_updated_queue, consumer, caplog
):
    catalog_updated_queue["client"].send_message(
        QueueUrl=catalog_updated_queue["queue_url"],
        MessageBody=json.dumps({"correlationId": "corr-from-event", "payload": {}}),
    )

    with caplog.at_level(logging.ERROR):
        consumer.poll_once(wait_time_seconds=0)

    [record] = [
        r for r in caplog.records
        if "catalog_updated_consumer failed to process message" in r.message
    ]
    assert record.correlationId == "corr-from-event"
