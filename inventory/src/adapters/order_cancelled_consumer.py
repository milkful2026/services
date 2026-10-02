"""SQS consumer for `OrderCancelled` (MA-118 FR-5) — same background-
thread SQS pattern and envelope convention as
adapters/zone_update_consumer.py: `{"payload": {...}, "correlationId": ...}`.

No real producer exists yet (Order Service, MA-97, not built) — this
consumer is implemented and tested against a fake/moto-published event
now, same documented posture MA-118 §11 Risk 1 already states for this
exact gap.

Payload contract (not specified further by MA-118 itself — defined here,
the minimum FR-5 needs): `{"payload": {"orderId": "..."}}`. Releases
every reservation still RESERVED for that `orderId` across every product
it touched (InventoryStockService.handle_order_cancelled), the same
effect as an explicit POST /inventory/release per product — idempotent
against redelivery (an order with nothing left to release is a no-op,
not an error).
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import InventoryError
from domain.inventory_stock_service import InventoryStockService

logger = logging.getLogger(__name__)


class OrderCancelledConsumer:
    def __init__(
        self,
        queue_url: str,
        stock_service: InventoryStockService,
        region_name: str,
        correlation_id: str = "",
    ) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._stock_service = stock_service
        self._correlation_id = correlation_id

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error(
                "order_cancelled_consumer.receive_message failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            return 0

        messages = response.get("Messages", [])
        for message in messages:
            self._process_message(message)
        return len(messages)

    def run_forever(self) -> None:
        while True:
            self.poll_once()

    def _process_message(self, message: dict) -> None:
        correlation_id = self._correlation_id
        try:
            body = json.loads(message["Body"])
            correlation_id = body.get("correlationId", self._correlation_id)
            order_id = body["payload"]["orderId"]
            if not isinstance(order_id, str) or not order_id:
                raise TypeError(f"orderId must be a non-empty string, got {order_id!r}")
            self._stock_service.handle_order_cancelled(order_id)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "order_cancelled_consumer failed to process message — left for retry/DLQ",
                extra={
                    "correlationId": correlation_id,
                    "messageId": message.get("MessageId"),
                    "error": str(exc),
                },
            )
            return
        except InventoryError as exc:
            # Transient (e.g. DB blip) — left in-queue for SQS's own
            # redelivery/DLQ rather than killing run_forever's thread.
            logger.error(
                "order_cancelled_consumer failed to release reservations — left for retry/DLQ",
                extra={
                    "correlationId": correlation_id,
                    "messageId": message.get("MessageId"),
                    "error": str(exc),
                },
            )
            return

        try:
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        except ClientError as exc:
            logger.error(
                "order_cancelled_consumer.delete_message failed",
                extra={"correlationId": correlation_id, "error": str(exc)},
            )
