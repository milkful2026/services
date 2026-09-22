"""OrderEventsConsumer dispatch + delete/no-delete behaviour, with a fake
SQS client and a real (SQLite-backed) OrderService."""

import json
from datetime import date

import pytest

from adapters.order_events_consumer import OrderEventsConsumer


class _FakeSqs:
    def __init__(self, messages):
        self._messages = messages
        self.deleted = []

    def receive_message(self, **_):
        msgs, self._messages = self._messages, []
        return {"Messages": msgs}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)


def _msg(detail_type, detail, rh="rh-1"):
    return {
        "MessageId": "m1",
        "ReceiptHandle": rh,
        "Body": json.dumps({"detail-type": detail_type, "detail": detail}),
    }


def _sod(**overrides):
    detail = {
        "eventId": "e1",
        "occurredAt": "2026-02-01T02:00:00+05:30",
        "subscriptionId": "sub-1",
        "userId": "user-1",
        "productId": "prod-1",
        "quantity": 2,
        "deliveryDate": "2026-02-01",
        "slotId": "slot-1",
        "correlationId": "corr-1",
    }
    detail.update(overrides)
    return detail


@pytest.fixture
def consumer_factory(service):
    def make(messages):
        c = OrderEventsConsumer.__new__(OrderEventsConsumer)
        c._sqs = _FakeSqs(messages)
        c._queue_url = "q"
        c._order_service = service
        return c

    return make


def test_subscription_order_due_materializes_and_acks(consumer_factory, repo):
    c = consumer_factory([_msg("SubscriptionOrderDue", _sod())])
    c.poll_once()
    order = repo.get_by_subscription_and_date("sub-1", date(2026, 2, 1))
    assert order is not None
    assert c._sqs.deleted == ["rh-1"]


def test_wallet_transport_failure_is_not_acked(consumer_factory, wallet_client):
    wallet_client.raise_unavailable = True
    c = consumer_factory([_msg("SubscriptionOrderDue", _sod())])
    c.poll_once()
    assert c._sqs.deleted == []  # left for redelivery / DLQ


def test_unknown_detail_type_is_acked(consumer_factory):
    c = consumer_factory([_msg("SomethingElse", {})])
    c.poll_once()
    assert c._sqs.deleted == ["rh-1"]


def test_schema_invalid_subscription_order_due_is_not_acked_and_does_not_crash(
    consumer_factory, repo
):
    # quantity as a string is a schema violation (the contract requires
    # an integer) — jsonschema.validate must raise, and that must be
    # caught rather than propagate out of poll_once and kill the consumer
    # loop; the poison message is left for redelivery/DLQ, not acked.
    # Mirrors the exact class of bug MA-24 PR #19's review found and
    # fixed in Wallet's own consumer.
    c = consumer_factory([_msg("SubscriptionOrderDue", _sod(quantity="not-a-number"))])
    c.poll_once()
    assert c._sqs.deleted == []
    assert repo.get_by_subscription_and_date("sub-1", date(2026, 2, 1)) is None
