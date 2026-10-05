import json
import logging

import pytest

from adapters.order_cancelled_consumer import OrderCancelledConsumer
from domain.exceptions import ServiceUnavailableError


class FakeStockService:
    def __init__(self):
        self.released: list[str] = []
        self.raises: Exception | None = None

    def handle_order_cancelled(self, order_ref):
        if self.raises is not None:
            raise self.raises
        self.released.append(order_ref)
        return []


def _send_order_cancelled(queue, order_id="order-1", correlation_id="corr-1") -> None:
    body = {"correlationId": correlation_id, "payload": {"orderId": order_id}}
    queue["client"].send_message(QueueUrl=queue["queue_url"], MessageBody=json.dumps(body))


@pytest.fixture
def stock_service():
    return FakeStockService()


@pytest.fixture
def consumer(order_cancelled_queue, stock_service):
    return OrderCancelledConsumer(
        queue_url=order_cancelled_queue["queue_url"],
        stock_service=stock_service,
        region_name="ap-south-1",
    )


def test_poll_once_releases_the_order(order_cancelled_queue, consumer, stock_service):
    _send_order_cancelled(order_cancelled_queue, order_id="order-1")

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.released == ["order-1"]


def test_poll_once_deletes_message_after_processing(order_cancelled_queue, consumer):
    _send_order_cancelled(order_cancelled_queue)
    consumer.poll_once(wait_time_seconds=0)

    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_poll_once_with_no_messages_returns_zero(consumer):
    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_malformed_message_is_left_in_queue_not_deleted(
    order_cancelled_queue, consumer, stock_service
):
    order_cancelled_queue["client"].send_message(
        QueueUrl=order_cancelled_queue["queue_url"], MessageBody="not valid json"
    )

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.released == []


def test_missing_order_id_does_not_raise(order_cancelled_queue, consumer, stock_service):
    order_cancelled_queue["client"].send_message(
        QueueUrl=order_cancelled_queue["queue_url"], MessageBody=json.dumps({"payload": {}})
    )

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert stock_service.released == []


def test_already_released_order_is_a_noop_not_an_error(
    order_cancelled_queue, consumer, stock_service
):
    # FR-5: OrderCancelled for an already-released reservation is a
    # no-op — modeled here by handle_order_cancelled() returning an
    # empty list (nothing left to release), same as a real repeat call.
    _send_order_cancelled(order_cancelled_queue)

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert stock_service.released == ["order-1"]
    # Message was deleted (no exception raised) — confirmed by the
    # second poll finding nothing.
    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_a_transient_failure_is_left_in_queue_not_raised(
    order_cancelled_queue, consumer, stock_service
):
    stock_service.raises = ServiceUnavailableError("db blip")
    _send_order_cancelled(order_cancelled_queue)

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert stock_service.released == []


def test_process_message_failure_logs_use_the_event_own_correlation_id(
    order_cancelled_queue, consumer, caplog
):
    order_cancelled_queue["client"].send_message(
        QueueUrl=order_cancelled_queue["queue_url"],
        MessageBody=json.dumps({"correlationId": "corr-from-event", "payload": {}}),
    )

    with caplog.at_level(logging.ERROR):
        consumer.poll_once(wait_time_seconds=0)

    [record] = [
        r for r in caplog.records
        if "order_cancelled_consumer failed to process message" in r.message
    ]
    assert record.correlationId == "corr-from-event"
