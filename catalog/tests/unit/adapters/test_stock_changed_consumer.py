import json
import logging

import pytest

from adapters.stock_changed_consumer import StockChangedConsumer


class FakeCatalogService:
    def __init__(self):
        self.calls: list[tuple] = []

    def apply_stock_change(self, product_id, event_id, stock_state, available_from):
        self.calls.append((product_id, event_id, stock_state, available_from))
        return True


def _send_stock_changed(sqs_queue, **payload_overrides) -> None:
    payload = {
        "eventId": "evt-1",
        "productId": "cow-milk",
        "availableQuantity": 10,
        "stockState": "OUT_OF_STOCK",
        "availableFrom": None,
        "occurredAt": "2026-08-16T10:00:00Z",
    }
    payload.update(payload_overrides)
    body = {"correlationId": "corr-1", "payload": payload}
    sqs_queue["client"].send_message(QueueUrl=sqs_queue["queue_url"], MessageBody=json.dumps(body))


@pytest.fixture
def catalog_service():
    return FakeCatalogService()


@pytest.fixture
def consumer(sqs_queue, catalog_service):
    return StockChangedConsumer(
        queue_url=sqs_queue["queue_url"], catalog_service=catalog_service, region_name="ap-south-1"
    )


def test_poll_once_applies_the_stock_change(sqs_queue, consumer, catalog_service):
    _send_stock_changed(sqs_queue)

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert catalog_service.calls == [("cow-milk", "evt-1", "OUT_OF_STOCK", None)]


def test_poll_once_parses_available_from_date(sqs_queue, consumer, catalog_service):
    _send_stock_changed(sqs_queue, stockState="AVAILABLE_FROM", availableFrom="2026-09-01")

    consumer.poll_once(wait_time_seconds=0)

    from datetime import date

    assert catalog_service.calls[0][3] == date(2026, 9, 1)


def test_poll_once_deletes_message_after_processing(sqs_queue, consumer):
    _send_stock_changed(sqs_queue)
    consumer.poll_once(wait_time_seconds=0)

    processed_again = consumer.poll_once(wait_time_seconds=0)

    assert processed_again == 0


def test_poll_once_with_no_messages_returns_zero(consumer):
    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_malformed_message_is_left_in_queue_not_deleted(sqs_queue, consumer, catalog_service):
    sqs_queue["client"].send_message(QueueUrl=sqs_queue["queue_url"], MessageBody="not valid json")

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert catalog_service.calls == []


def test_missing_payload_field_does_not_raise(sqs_queue, consumer, catalog_service):
    sqs_queue["client"].send_message(
        QueueUrl=sqs_queue["queue_url"], MessageBody=json.dumps({"payload": {}})
    )

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert catalog_service.calls == []


def test_invalid_available_from_date_is_rejected_not_applied(sqs_queue, consumer, catalog_service):
    _send_stock_changed(sqs_queue, availableFrom="not-a-date")

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert catalog_service.calls == []


def test_process_message_failure_logs_use_the_event_own_correlation_id(
    sqs_queue, consumer, caplog
):
    sqs_queue["client"].send_message(
        QueueUrl=sqs_queue["queue_url"],
        MessageBody=json.dumps({"correlationId": "corr-from-event", "payload": {}}),
    )

    with caplog.at_level(logging.ERROR):
        consumer.poll_once(wait_time_seconds=0)

    [record] = [
        r
        for r in caplog.records
        if "stock_changed_consumer failed to process message" in r.message
    ]
    assert record.correlationId == "corr-from-event"
