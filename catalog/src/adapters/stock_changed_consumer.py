"""SQS consumer for `StockChanged` (MA-116 FR-5) — mirrors Inventory's own
`StockChanged`/`OrderCancelled` event-envelope convention exactly
(services/inventory/src/adapters/zone_update_consumer.py's
`{"payload": {...}}` shape), since that's the domain-event envelope this
platform uses throughout (services/README.md §5).

Payload contract, agreed jointly with MA-95/MA-118 during SDD review (see
MA-116 §FR-5 / MA-118 §FR-6 for the identical documented shape):

    {
      "eventId": "uuid",
      "productId": "uuid",
      "availableQuantity": 0,
      "stockState": "IN_STOCK | OUT_OF_STOCK | AVAILABLE_FROM",
      "availableFrom": "2026-09-01" | null,
      "occurredAt": "2026-08-16T10:00:00Z"
    }

No real producer exists yet — Inventory's reserve/commit/release
(MA-118) hasn't been implemented, only spec'd. This consumer is
implemented and tested against a fake/moto-published event now so the
contract is proven ahead of that landing, not blocked on it.
"""

import json
import logging
from datetime import date

import boto3
from botocore.exceptions import ClientError

from domain.catalog_service import CatalogService

logger = logging.getLogger(__name__)


class StockChangedConsumer:
    def __init__(
        self,
        queue_url: str,
        catalog_service: CatalogService,
        region_name: str,
        correlation_id: str = "",
    ) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._catalog_service = catalog_service
        self._correlation_id = correlation_id

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        """Receives and processes up to `max_messages`. Returns the count
        received (not necessarily all successfully processed — malformed
        messages are left in-queue to retry, then DLQ per the queue's
        redrive policy)."""
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error(
                "stock_changed_consumer.receive_message failed",
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
            payload = body["payload"]
            event_id = payload["eventId"]
            product_id = payload["productId"]
            stock_state = payload["stockState"]
            available_from_raw = payload.get("availableFrom")
            available_from = (
                date.fromisoformat(available_from_raw) if available_from_raw else None
            )
            self._catalog_service.apply_stock_change(
                product_id=product_id,
                event_id=event_id,
                stock_state=stock_state,
                available_from=available_from,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "stock_changed_consumer failed to process message — left for retry/DLQ",
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
                "stock_changed_consumer.delete_message failed",
                extra={"correlationId": correlation_id, "error": str(exc)},
            )
