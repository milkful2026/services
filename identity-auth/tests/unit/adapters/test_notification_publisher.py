import json

import boto3
import pytest
from botocore.exceptions import ClientError

from adapters.notification_publisher import EventBridgeNotificationPublisher
from domain.exceptions import NotificationPublishError


@pytest.fixture
def publisher(event_bus):
    return EventBridgeNotificationPublisher(
        event_bus_name="default",
        event_source="identity-auth",
        region_name="ap-south-1",
        max_retries=2,
        backoff_base_seconds=0.01,
    )


def test_publish_otp_requested_succeeds(publisher):
    # No exception raised == success against moto's default bus.
    publisher.publish_otp_requested("+919876543210", "123456", "corr-1")


def test_publish_otp_requested_uses_fixed_event_envelope(publisher, monkeypatch):
    captured = {}

    def _capture(**kwargs):
        entry = kwargs["Entries"][0]
        captured["entry"] = entry
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "evt-1"}]}

    monkeypatch.setattr(publisher._client, "put_events", _capture)

    publisher.publish_otp_requested("+919876543210", "123456", "corr-1")

    entry = captured["entry"]
    assert entry["Source"] == "identity-auth"
    assert entry["DetailType"] == "identity.otp.requested"
    assert entry["EventBusName"] == "default"

    detail = json.loads(entry["Detail"])
    assert detail["eventType"] == "identity.otp.requested"
    assert detail["eventVersion"] == "1.0"
    assert detail["source"] == "identity-auth"
    assert detail["correlationId"] == "corr-1"
    assert detail["payload"] == {"mobile": "+919876543210", "otp": "123456"}
    assert "eventId" in detail
    assert "timestamp" in detail


def test_publish_retries_then_raises_on_persistent_client_error(publisher, monkeypatch):
    calls = {"count": 0}

    def _raise(**kwargs):
        calls["count"] += 1
        raise ClientError({"Error": {"Code": "InternalException"}}, "PutEvents")

    monkeypatch.setattr(publisher._client, "put_events", _raise)

    with pytest.raises(NotificationPublishError):
        publisher.publish_otp_requested("+919876543210", "123456", "corr-1")

    # Initial attempt + 2 retries = 3 total calls.
    assert calls["count"] == 3


def test_publish_retries_then_raises_on_failed_entry(publisher, monkeypatch):
    def _fail_entry(**kwargs):
        return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}

    monkeypatch.setattr(publisher._client, "put_events", _fail_entry)

    with pytest.raises(NotificationPublishError) as exc_info:
        publisher.publish_otp_requested("+919876543210", "123456", "corr-1")

    # A partial-batch failure (no ClientError raised) must still surface the
    # real per-entry error in `cause`, not the literal string "None".
    assert "InternalFailure" in exc_info.value.details["cause"]


def test_publish_succeeds_after_transient_failure(publisher, monkeypatch):
    calls = {"count": 0}

    def _flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            raise ClientError({"Error": {"Code": "InternalException"}}, "PutEvents")
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "evt-1"}]}

    monkeypatch.setattr(publisher._client, "put_events", _flaky)

    publisher.publish_otp_requested("+919876543210", "123456", "corr-1")

    assert calls["count"] == 2
