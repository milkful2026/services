"""EventBridgeStockEventPublisher — verifies the SQS-delivered envelope
shape directly (publish through a real EventBridge rule + SQS target
against moto, the same path a real deployment uses), not just that
`put_events` was called. This is the exact gap a live local-dev
walkthrough caught (see stock_event_publisher.py's module docstring):
publishing FR-6's fields flat, as its own JSON example literally shows,
reaches a real SQS consumer as `body["detail"][...]`, not
`body["payload"][...]` — every existing consumer in this codebase
(zone_update_consumer.py, catalog's stock_changed_consumer.py) expects
the latter. These tests assert the actual wire shape a consumer would
parse, round-tripped through real EventBridge rule-matching and an
InputTransformer-equipped SQS target, so a future regression here would
be caught by the test suite instead of only a live walkthrough."""

import json

import boto3
import pytest
from moto import mock_aws

from adapters.stock_event_publisher import EventBridgeStockEventPublisher
from domain.models import StockState, StockSummary


@pytest.fixture
def wired_queue():
    with mock_aws():
        sqs = boto3.client("sqs", region_name="ap-south-1")
        events = boto3.client("events", region_name="ap-south-1")
        queue = sqs.create_queue(QueueName="stock-changed-test")
        queue_arn = sqs.get_queue_attributes(
            QueueUrl=queue["QueueUrl"], AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        events.put_rule(
            Name="StockChangedTestRule",
            EventPattern=json.dumps(
                {"source": ["inventory"], "detail-type": ["inventory.stock.changed"]}
            ),
            State="ENABLED",
        )
        events.put_targets(
            Rule="StockChangedTestRule",
            Targets=[
                {
                    "Id": "stock-changed-test-target",
                    "Arn": queue_arn,
                    "InputTransformer": {
                        "InputPathsMap": {"detail": "$.detail"},
                        "InputTemplate": "<detail>",
                    },
                }
            ],
        )
        yield {"sqs": sqs, "queue_url": queue["QueueUrl"]}


@pytest.fixture
def publisher():
    return EventBridgeStockEventPublisher(
        event_bus_name="default", event_source="inventory", region_name="ap-south-1"
    )


def _summary(**overrides) -> StockSummary:
    defaults = dict(
        product_id="cow-milk", on_hand=50, reserved=0, available=50,
        low_stock_threshold=10, stock_state=StockState.IN_STOCK, available_from=None,
    )
    defaults.update(overrides)
    return StockSummary(**defaults)


def test_stock_changed_delivers_fields_nested_under_payload(wired_queue, publisher):
    publisher.publish_stock_changed(_summary())

    messages = wired_queue["sqs"].receive_message(
        QueueUrl=wired_queue["queue_url"], MaxNumberOfMessages=1, WaitTimeSeconds=0
    )["Messages"]

    body = json.loads(messages[0]["Body"])
    payload = body["payload"]  # the exact shape every existing consumer parses
    assert payload["productId"] == "cow-milk"
    assert payload["availableQuantity"] == 50
    assert payload["stockState"] == "IN_STOCK"
    assert payload["availableFrom"] is None
    assert "eventId" in payload  # FR-6: fresh uuid per publish
    assert "occurredAt" in payload
    assert body["correlationId"] == payload["eventId"]
    # Regression: EventBridgeOutboxPublisher.publish()'s own
    # detail.setdefault("eventId", ...) stamps the *envelope* dict (what
    # this method calls `detail`), not `payload` — since the envelope had
    # no top-level eventId/occurredAt of its own, that setdefault
    # previously added a SECOND, different eventId/occurredAt at the
    # envelope's top level, never matching payload["eventId"] (the one
    # every consumer's dedup logic actually reads).
    assert body["eventId"] == payload["eventId"]
    assert body["occurredAt"] == payload["occurredAt"]


def test_stock_changed_available_from_is_isoformat_date_string(wired_queue, publisher):
    import datetime

    publisher.publish_stock_changed(
        _summary(available=0, stock_state=StockState.AVAILABLE_FROM,
                  available_from=datetime.date(2026, 9, 1))
    )

    messages = wired_queue["sqs"].receive_message(
        QueueUrl=wired_queue["queue_url"], MaxNumberOfMessages=1, WaitTimeSeconds=0
    )["Messages"]
    payload = json.loads(messages[0]["Body"])["payload"]

    assert payload["availableFrom"] == "2026-09-01"


def test_each_publish_gets_a_fresh_event_id(wired_queue, publisher):
    publisher.publish_stock_changed(_summary())
    publisher.publish_stock_changed(_summary())

    messages = wired_queue["sqs"].receive_message(
        QueueUrl=wired_queue["queue_url"], MaxNumberOfMessages=10, WaitTimeSeconds=0
    )["Messages"]

    event_ids = {json.loads(m["Body"])["payload"]["eventId"] for m in messages}
    assert len(event_ids) == 2
