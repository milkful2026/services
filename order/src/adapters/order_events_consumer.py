"""SQS consumer for `order-events-q` (owned by this service).

Consumes only `SubscriptionOrderDue` — anything else is acked + logged
as unhandled. A message that raises any `OrderError` (e.g.
`AddressLookupUnavailableError`, `PricingUnavailableError`,
`WalletUnavailableError` — all transient) is NOT deleted: SQS redelivers
it, and after the redrive count it lands in the DLQ with an alarm. It is
never acked-and-dropped. Same shape as wallet's own
`wallet_events_consumer.py`, simplified to one event type."""

import json
import logging
from datetime import date

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import OrderError
from domain.order_service import OrderService

logger = logging.getLogger(__name__)

try:  # optional in prod images; present in dev for contract validation
    import jsonschema
    from shared.events import load_schema  # type: ignore

    _SUBSCRIPTION_ORDER_DUE_SCHEMA = load_schema("SubscriptionOrderDue")
    # An empty tuple as the "no jsonschema" fallback means the except
    # clause naming this never matches anything, rather than raising
    # AttributeError looking up .ValidationError on None the first time
    # some *other* exception is being matched against it.
    _SCHEMA_VALIDATION_ERROR: type[Exception] | tuple[()] = jsonschema.ValidationError
except Exception:  # noqa: BLE001
    jsonschema = None
    _SUBSCRIPTION_ORDER_DUE_SCHEMA = None
    _SCHEMA_VALIDATION_ERROR = ()


class OrderEventsConsumer:
    def __init__(self, queue_url: str, order_service: OrderService, region_name: str) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._order_service = order_service

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error("order_events_consumer.receive_message failed", extra={"error": str(exc)})
            return 0
        messages = response.get("Messages", [])
        for message in messages:
            self._process_message(message)
        return len(messages)

    def run_forever(self) -> None:
        while True:
            self.poll_once()

    def _process_message(self, message: dict) -> None:
        try:
            envelope = json.loads(message["Body"])
            detail_type = envelope.get("detail-type") or envelope.get("detailType")
            detail = envelope.get("detail", {})
            self._dispatch(detail_type, detail)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "order_events_consumer: malformed message — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except OrderError as exc:
            logger.error(
                "order_events_consumer: transient processing failure — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except _SCHEMA_VALIDATION_ERROR as exc:
            logger.error(
                "order_events_consumer: schema-invalid SubscriptionOrderDue — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return

        try:
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        except ClientError as exc:
            logger.error("order_events_consumer.delete_message failed", extra={"error": str(exc)})

    def _dispatch(self, detail_type: str | None, detail: dict) -> None:
        if detail_type == "SubscriptionOrderDue":
            if jsonschema is not None and _SUBSCRIPTION_ORDER_DUE_SCHEMA is not None:
                jsonschema.validate(detail, _SUBSCRIPTION_ORDER_DUE_SCHEMA)
            self._order_service.materialize(
                subscription_id=detail["subscriptionId"],
                user_id=detail["userId"],
                product_id=detail["productId"],
                quantity=detail["quantity"],
                delivery_date=date.fromisoformat(detail["deliveryDate"]),
                slot_id=detail["slotId"],
                correlation_id=detail.get("correlationId"),
            )
            return
        logger.info("order_events_consumer: unhandled", extra={"dt": detail_type})
