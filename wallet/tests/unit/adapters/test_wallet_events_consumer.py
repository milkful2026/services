"""WalletEventsConsumer dispatch + delete/no-delete behaviour, with a
fake SQS client and a real (SQLite-backed) WalletService."""

import json

import pytest

from adapters.wallet_events_consumer import WalletEventsConsumer, _unwrap_legacy_envelope
from tests.conftest import seed_wallet


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


def _pc(**o):
    d = {
        "eventId": "e1",
        "occurredAt": "2026-09-11T10:00:00+00:00",
        "correlationId": "c1",
        "paymentId": "pay_1",
        "userId": "user-1",
        "purpose": "WALLET_RECHARGE",
        "amountPaise": 50000,
        "currency": "INR",
        "method": "UPI",
        "razorpayPaymentId": "rzp_1",
        "razorpayOrderId": "ord_1",
    }
    d.update(o)
    return d


@pytest.fixture
def consumer_factory(service):
    def make(messages):
        c = WalletEventsConsumer.__new__(WalletEventsConsumer)
        c._sqs = _FakeSqs(messages)
        c._queue_url = "q"
        c._wallet_service = service
        return c

    return make


def test_user_registered_creates_wallet_and_acks(consumer_factory, repo):
    c = consumer_factory([_msg("UserRegistered", {"userId": "user-1"})])
    c.poll_once()
    assert repo.get_wallet_by_user("user-1") is not None
    assert c._sqs.deleted == ["rh-1"]


def test_user_registered_real_envelope_shape_creates_wallet_and_acks(consumer_factory, repo):
    # Regression (MA-134): User Service publishes UserRegistered through
    # its own, older adapters/outbox_event_publisher.py (predates
    # shared/'s), which wraps the actual domain payload one level deeper
    # than every other event this consumer handles —
    # {eventId, eventType, eventVersion, source, timestamp, correlationId,
    # payload: {...}} — not a flat `{"userId": ...}` detail as the test
    # above (and this consumer's own dispatch logic, before this fix)
    # assumed. A hand-rolled flat detail here masked the mismatch from
    # ever being caught until a real docker-compose run surfaced it.
    real_detail = {
        "eventId": "0f00136f-3b79-413f-8ae6-31b7f712b02e",
        "eventType": "UserRegistered",
        "eventVersion": "1.0",
        "source": "user",
        "timestamp": "2026-09-23T13:57:36.528341+00:00",
        "correlationId": "13b32911-106b-4fa0-b307-a03206fbeb40",
        "payload": {
            "mobile": "+919812340088",
            "userId": "user-1",
            "defaultPincode": "560001",
        },
    }
    c = consumer_factory([_msg("UserRegistered", real_detail)])
    c.poll_once()
    assert repo.get_wallet_by_user("user-1") is not None
    assert c._sqs.deleted == ["rh-1"]


def test_unwrap_legacy_envelope_is_generic_not_userregistered_specific():
    # Regression: the unwrap must apply to *any* detail_type, not just
    # UserRegistered — otherwise the next event type this consumer
    # subscribes to from a legacy-shaped publisher (User Service's own,
    # or Cart's identical copy) reintroduces the same KeyError this PR
    # fixed, just for a different event.
    legacy = {
        "eventId": "e1",
        "eventType": "SomeFutureEvent",
        "source": "user",
        "correlationId": "c1",
        "payload": {"userId": "user-1", "someField": "x"},
    }
    assert _unwrap_legacy_envelope(legacy) == {"userId": "user-1", "someField": "x"}


def test_unwrap_legacy_envelope_leaves_flat_detail_unchanged():
    flat = {"userId": "user-1", "amountPaise": 100}
    assert _unwrap_legacy_envelope(flat) == flat


def test_unwrap_legacy_envelope_ignores_a_non_dict_payload_field():
    # A real domain field literally named "payload" would be unusual
    # (no schema in this codebase has one), but if it were ever a
    # non-dict scalar, that's not the legacy envelope shape — leave
    # `detail` untouched rather than unwrapping onto a non-dict.
    detail = {"userId": "user-1", "payload": "not-a-dict"}
    assert _unwrap_legacy_envelope(detail) == detail


def test_recharge_credits_and_acks(consumer_factory, engine, repo):
    seed_wallet(engine, balance_paise=1000)
    c = consumer_factory([_msg("PaymentConfirmed", _pc())])
    c.poll_once()
    assert repo.get_wallet_by_user("user-1").balance_paise == 51000
    assert c._sqs.deleted == ["rh-1"]


def test_duplicate_recharge_single_credit(consumer_factory, engine, repo):
    seed_wallet(engine, balance_paise=1000)
    consumer_factory([_msg("PaymentConfirmed", _pc(), rh="a")]).poll_once()
    consumer_factory([_msg("PaymentConfirmed", _pc(), rh="b")]).poll_once()
    assert repo.get_wallet_by_user("user-1").balance_paise == 51000
    assert len(repo.fetch_unpublished()) == 1


def test_recharge_before_wallet_is_not_acked(consumer_factory):
    c = consumer_factory([_msg("PaymentConfirmed", _pc(userId="ghost"))])
    c.poll_once()
    assert c._sqs.deleted == []  # left for redelivery / DLQ


def test_order_purpose_is_acked_no_credit(consumer_factory, engine, repo):
    seed_wallet(engine, balance_paise=1000)
    c = consumer_factory([_msg("PaymentConfirmed", _pc(purpose="ORDER"))])
    c.poll_once()
    assert repo.get_wallet_by_user("user-1").balance_paise == 1000
    assert c._sqs.deleted == ["rh-1"]


def test_unknown_detail_type_is_acked(consumer_factory):
    c = consumer_factory([_msg("SomethingElse", {})])
    c.poll_once()
    assert c._sqs.deleted == ["rh-1"]


def test_schema_invalid_payment_confirmed_is_not_acked_and_does_not_crash(
    consumer_factory, engine, repo
):
    # amountPaise as a string is a schema violation (the contract requires
    # an integer) — jsonschema.validate must raise, and that must be
    # caught rather than propagate out of poll_once and kill the consumer
    # loop; the poison message is left for redelivery/DLQ, not acked.
    seed_wallet(engine, balance_paise=1000)
    c = consumer_factory([_msg("PaymentConfirmed", _pc(amountPaise="not-a-number"))])
    c.poll_once()
    assert c._sqs.deleted == []
    assert repo.get_wallet_by_user("user-1").balance_paise == 1000
