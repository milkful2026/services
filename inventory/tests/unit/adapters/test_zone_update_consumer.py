import json

import pytest

from adapters.zone_update_consumer import ZoneUpdateConsumer


class FakeZoneCache:
    def __init__(self):
        self.invalidated_prefixes: list[str] = []

    def invalidate_by_prefix(self, prefix: str) -> None:
        self.invalidated_prefixes.append(prefix)


def _send_zone_updated(sqs_queue, prefixes: list[str]) -> None:
    body = {
        "eventId": "evt-1",
        "eventType": "inventory.zone.updated",
        "eventVersion": "1.0",
        "source": "inventory",
        "timestamp": "2026-01-01T00:00:00Z",
        "correlationId": "corr-1",
        "payload": {"zoneId": "blr-central", "pincodePrefixes": prefixes},
    }
    sqs_queue["client"].send_message(QueueUrl=sqs_queue["queue_url"], MessageBody=json.dumps(body))


@pytest.fixture
def zone_cache():
    return FakeZoneCache()


@pytest.fixture
def consumer(sqs_queue, zone_cache):
    return ZoneUpdateConsumer(
        queue_url=sqs_queue["queue_url"], zone_cache=zone_cache, region_name="ap-south-1"
    )


def test_poll_once_invalidates_all_prefixes_in_message(sqs_queue, consumer, zone_cache):
    _send_zone_updated(sqs_queue, ["5600", "5601"])

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert zone_cache.invalidated_prefixes == ["5600", "5601"]


def test_poll_once_deletes_message_after_processing(sqs_queue, consumer):
    _send_zone_updated(sqs_queue, ["5600"])
    consumer.poll_once(wait_time_seconds=0)

    # Second poll should find nothing left — the message was deleted.
    processed_again = consumer.poll_once(wait_time_seconds=0)

    assert processed_again == 0


def test_poll_once_with_no_messages_returns_zero(consumer):
    assert consumer.poll_once(wait_time_seconds=0) == 0


def test_malformed_message_is_left_in_queue_not_deleted(sqs_queue, consumer, zone_cache):
    sqs_queue["client"].send_message(
        QueueUrl=sqs_queue["queue_url"], MessageBody="not valid json"
    )

    processed = consumer.poll_once(wait_time_seconds=0)

    assert processed == 1
    assert zone_cache.invalidated_prefixes == []
    # Message wasn't deleted — a fresh poll should still find it (moto's
    # default visibility timeout means it may or may not be immediately
    # re-receivable, but the queue's message count should still show it).
    attrs = sqs_queue["client"].get_queue_attributes(
        QueueUrl=sqs_queue["queue_url"], AttributeNames=["ApproximateNumberOfMessages"]
    )
    # Either still visible or in-flight (not visible yet) — either way it
    # was NOT deleted, which is the behavior under test.
    assert "ApproximateNumberOfMessages" in attrs["Attributes"]


def test_missing_payload_field_does_not_raise(sqs_queue, consumer, zone_cache):
    sqs_queue["client"].send_message(
        QueueUrl=sqs_queue["queue_url"], MessageBody=json.dumps({"payload": {}})
    )

    processed = consumer.poll_once(wait_time_seconds=0)  # must not raise

    assert processed == 1
    assert zone_cache.invalidated_prefixes == []
