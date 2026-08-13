import json

import pytest
from botocore.exceptions import ClientError

from adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def publisher(event_bus):
    return EventBridgeOutboxPublisher(
        event_bus_name="default", event_source="user", region_name="ap-south-1",
        max_retries=2, backoff_base_seconds=0.01,
    )


def test_publish_succeeds(publisher):
    publisher.publish("UserRegistered", {"cognitoSub": "sub-1"}, "corr-1")


def test_publish_uses_fixed_event_envelope(publisher, monkeypatch):
    captured = {}

    def _capture(**kwargs):
        captured["entry"] = kwargs["Entries"][0]
        return {"FailedEntryCount": 0}

    monkeypatch.setattr(publisher._client, "put_events", _capture)

    publisher.publish("UserRegistered", {"cognitoSub": "sub-1"}, "corr-1")

    entry = captured["entry"]
    assert entry["Source"] == "user"
    assert entry["DetailType"] == "UserRegistered"
    detail = json.loads(entry["Detail"])
    assert detail["eventType"] == "UserRegistered"
    assert detail["correlationId"] == "corr-1"
    assert detail["payload"] == {"cognitoSub": "sub-1"}


def test_publish_retries_then_raises_on_persistent_client_error(publisher, monkeypatch):
    calls = {"count": 0}

    def _raise(**kwargs):
        calls["count"] += 1
        raise ClientError({"Error": {"Code": "InternalException"}}, "PutEvents")

    monkeypatch.setattr(publisher._client, "put_events", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        publisher.publish("UserRegistered", {}, "corr-1")

    assert calls["count"] == 3
