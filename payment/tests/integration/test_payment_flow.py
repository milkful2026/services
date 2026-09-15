"""End-to-end within this service's own boundary: HTTP create -> confirm
-> signed webhook -> outbox -> EventBridge rule -> SQS, validated against
the shared PaymentConfirmed schema. Uses the FakeGateway (no real
Razorpay credentials — see MA-24 implementation notes on deferring the
real-Razorpay-test-mode pass until those are provided) plus moto for
EventBridge/SQS, so it exercises every seam this service owns without
external network access.
"""

import json

import boto3
import jsonschema
import jwt
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from shared.events import load_schema

from adapters.outbox_event_publisher import EventBridgeOutboxPublisher
from adapters.payment_repository import SqlAlchemyPaymentRepository
from handlers.app import app
from handlers.dependencies import get_payment_service


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


@pytest.fixture
def client(service):
    get_payment_service.cache_clear()
    app.dependency_overrides[get_payment_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_recharge_confirmed_reaches_wallet_events_queue(client, repo, engine):
    created = client.post(
        "/payments",
        headers={**_bearer(), "Idempotency-Key": "k1"},
        json={"purpose": "WALLET_RECHARGE", "amountPaise": 75000, "method": "UPI"},
    ).json()["data"]

    confirm = client.post(
        f"/payments/{created['paymentId']}/confirm",
        headers=_bearer(),
        json={
            "razorpayPaymentId": "rzp_pay_1",
            "razorpayOrderId": created["razorpayOrderId"],
            "razorpaySignature": "sig",
        },
    )
    assert confirm.json()["data"]["status"] == "CONFIRMING"

    webhook_body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "rzp_pay_1",
                        "order_id": created["razorpayOrderId"],
                        "amount": 75000,
                        "method": "upi",
                    }
                }
            },
        }
    ).encode()
    webhook_resp = client.post(
        "/payments/webhook", content=webhook_body, headers={"X-Razorpay-Signature": "any"}
    )
    assert webhook_resp.status_code == 200
    assert repo.get(created["paymentId"]).status.value == "CONFIRMED"

    unpublished = repo.fetch_unpublished()
    assert len(unpublished) == 1
    detail = unpublished[0]["payload"]
    jsonschema.validate(detail, load_schema("PaymentConfirmed"))
    assert detail["purpose"] == "WALLET_RECHARGE"
    assert detail["amountPaise"] == 75000

    with mock_aws():
        events = boto3.client("events", region_name="ap-south-1")
        sqs = boto3.client("sqs", region_name="ap-south-1")
        events.create_event_bus(Name="milkful-events")
        queue_url = sqs.create_queue(QueueName="wallet-events-q")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        sqs.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "events.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": queue_arn,
                            }
                        ],
                    }
                )
            },
        )
        events.put_rule(
            Name="payment-confirmed-wallet-recharge",
            EventBusName="milkful-events",
            EventPattern=json.dumps(
                {"detail-type": ["PaymentConfirmed"], "detail": {"purpose": ["WALLET_RECHARGE"]}}
            ),
        )
        events.put_targets(
            Rule="payment-confirmed-wallet-recharge",
            EventBusName="milkful-events",
            Targets=[{"Id": "wallet-events-q", "Arn": queue_arn}],
        )

        publisher = EventBridgeOutboxPublisher(
            event_bus_name="milkful-events",
            event_source="milkful.payment",
            region_name="ap-south-1",
        )
        repo2 = SqlAlchemyPaymentRepository(engine)
        for row in repo2.fetch_unpublished():
            publisher.publish(row["event_type"], row["payload"])
            repo2.mark_published(row["id"])

        messages = sqs.receive_message(
            QueueUrl=queue_url, WaitTimeSeconds=1, MaxNumberOfMessages=10
        )
        received = messages.get("Messages", [])
        assert len(received) == 1
        envelope = json.loads(received[0]["Body"])
        assert envelope["detail-type"] == "PaymentConfirmed"
        assert envelope["detail"]["purpose"] == "WALLET_RECHARGE"
