"""UserStatusChangedConsumer dispatch + delete/no-delete behaviour, with
a fake SQS client and a real (SQLite-backed) SubscriptionService —
mirrors wallet's own test_wallet_events_consumer.py."""

import json
from datetime import date, datetime, time, timedelta

import pytest

from adapters.user_status_changed_consumer import UserStatusChangedConsumer, _unwrap_envelope
from domain.models import IST, Schedule, ScheduleType, SubscriptionStatus

TODAY = date(2026, 1, 15)
AFTER_CUTOFF = datetime.combine(TODAY, time(21, 0), tzinfo=IST)


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


def _user_status_changed_payload(**overrides):
    payload = {
        "userId": "user-1",
        "previousStatus": "Active",
        "newStatus": "Deactivated",
        "reason": "closure requested",
        "effectiveFrom": "2026-09-30",
        "actorAdminId": "admin-1",
    }
    payload.update(overrides)
    return payload


def _real_envelope(payload: dict) -> dict:
    """Same nested-payload shape User Service's own (pre-shared/)
    adapters/outbox_event_publisher.py produces for every event it
    publishes, including user.status.changed — see this consumer's own
    _unwrap_envelope docstring."""
    return {
        "eventId": "0f00136f-3b79-413f-8ae6-31b7f712b02e",
        "eventType": "user.status.changed",
        "eventVersion": "1.0",
        "source": "user",
        "timestamp": "2026-09-30T13:57:36.528341+00:00",
        "correlationId": "13b32911-106b-4fa0-b307-a03206fbeb40",
        "payload": payload,
    }


@pytest.fixture
def consumer_factory(service):
    def make(messages):
        c = UserStatusChangedConsumer.__new__(UserStatusChangedConsumer)
        c._sqs = _FakeSqs(messages)
        c._queue_url = "q"
        c._subscription_service = service
        return c

    return make


def _create_sub(service, **overrides):
    kwargs = dict(
        user_id="user-1",
        product_id="prod-1",
        quantity=1,
        schedule=Schedule(type=ScheduleType.DAILY),
        start_date=TODAY,
        slot_id="slot-1",
        idempotency_key="key-1",
        correlation_id=None,
        now=AFTER_CUTOFF,
    )
    kwargs.update(overrides)
    return service.create(**kwargs)["subscriptionId"]


def test_unwrap_envelope_extracts_nested_payload():
    real = _real_envelope(_user_status_changed_payload())
    assert _unwrap_envelope(real) == _user_status_changed_payload()


def test_unwrap_envelope_passes_through_a_flat_detail():
    flat = _user_status_changed_payload()
    assert _unwrap_envelope(flat) == flat


def test_deactivated_pauses_subscription_and_acks(consumer_factory, service, repo):
    sub_id = _create_sub(service)
    c = consumer_factory(
        [_msg("user.status.changed", _real_envelope(_user_status_changed_payload()))]
    )

    c.poll_once()

    assert repo.get_by_id(sub_id).status == SubscriptionStatus.PAUSED
    assert c._sqs.deleted == ["rh-1"]


def test_suspended_pauses_subscription(consumer_factory, service, repo):
    sub_id = _create_sub(service)
    c = consumer_factory(
        [
            _msg(
                "user.status.changed",
                _real_envelope(_user_status_changed_payload(newStatus="Suspended")),
            )
        ]
    )

    c.poll_once()
    assert repo.get_by_id(sub_id).status == SubscriptionStatus.PAUSED


def test_active_reactivation_is_ignored_no_auto_resume(consumer_factory, service, repo):
    sub_id = _create_sub(service)
    service.pause_for_account_status_change(sub_id, "account_deactivated", now=AFTER_CUTOFF)
    c = consumer_factory(
        [
            _msg(
                "user.status.changed",
                _real_envelope(_user_status_changed_payload(newStatus="Active")),
            )
        ]
    )

    c.poll_once()

    # Still paused — this consumer is one-directional (MA-39 D2).
    assert repo.get_by_id(sub_id).status == SubscriptionStatus.PAUSED
    assert c._sqs.deleted == ["rh-1"]  # still acked — not an error, just a no-op


def test_zero_subscriptions_for_user_is_not_an_error(consumer_factory, repo):
    c = consumer_factory(
        [
            _msg(
                "user.status.changed",
                _real_envelope(_user_status_changed_payload(userId="user-with-no-subs")),
            )
        ]
    )

    c.poll_once()
    assert c._sqs.deleted == ["rh-1"]


def test_redelivered_event_is_a_safe_noop(consumer_factory, service, repo):
    sub_id = _create_sub(service)
    c = consumer_factory(
        [_msg("user.status.changed", _real_envelope(_user_status_changed_payload()), rh="rh-1")]
    )
    c.poll_once()
    assert repo.get_by_id(sub_id).status == SubscriptionStatus.PAUSED

    c2 = consumer_factory(
        [_msg("user.status.changed", _real_envelope(_user_status_changed_payload()), rh="rh-2")]
    )
    c2.poll_once()  # redelivery — must not raise or double-pause
    assert repo.get_by_id(sub_id).status == SubscriptionStatus.PAUSED
    assert c2._sqs.deleted == ["rh-2"]


def test_unknown_detail_type_is_ignored_and_acked(consumer_factory):
    c = consumer_factory([_msg("SomeOtherEvent", {"foo": "bar"})])
    c.poll_once()
    assert c._sqs.deleted == ["rh-1"]


def test_malformed_body_is_left_for_retry_not_acked(consumer_factory):
    c = consumer_factory(
        [{"MessageId": "m1", "ReceiptHandle": "rh-1", "Body": "not-json"}]
    )
    c.poll_once()
    assert c._sqs.deleted == []
