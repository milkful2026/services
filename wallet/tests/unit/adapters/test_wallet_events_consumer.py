"""WalletEventsConsumer dispatch + delete/no-delete behaviour, with a
fake SQS client and a real (SQLite-backed) WalletService."""

import json

import pytest

from adapters.wallet_events_consumer import WalletEventsConsumer
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
