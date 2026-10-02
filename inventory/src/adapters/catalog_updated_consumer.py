"""SQS consumer for `CatalogUpdated` (MA-118 FR-8) — same background-
thread SQS pattern and envelope convention as
adapters/zone_update_consumer.py / adapters/order_cancelled_consumer.py.

**Flagged deviation from the implementation plan, not a silent addition**
(see this story's PR description): the impl-plan's 9 steps (§3) do not
list a `CatalogUpdated` consumer as its own step, but MA-118 FR-8 is
in-scope regardless — it is the *only* documented mechanism by which a
`stock` row ever comes to exist (every other endpoint here, including
MA-150's `receive()`, 404s on an unprovisioned product). Built as part
of this step since nothing downstream (reserve, adjust, receive) is
reachable for a real product without it.

**Worse than FR-8's own documented posture**: FR-8 and MA-118 §8 both
describe this as "no real producer exists yet" in the same breath as
`OrderCancelled` (Order Service not built) — but unlike Order Service,
Catalog *is* fully built and running; it simply has no outbox/event-
publish mechanism of any kind today (confirmed by reading
catalog/src/domain/catalog_service.py and catalog/src/adapters/ — no
`outbox` table, no `EventBridgeOutboxPublisher` usage, nothing publishes
on product create). Building that publish side is Catalog's own scope
(MA-94/MA-116), not this story's — out of scope here, same as this
service never reaching into Catalog's database. This consumer is
implemented and tested against a fake/moto-published event, and
exercised locally via a small local-dev-only script (see
local-dev/seed_inventory_stock.py) that publishes a CatalogUpdated-
shaped event through the real local EventBridge/SQS wiring, rather than
Catalog's own code (which cannot yet do this for real).

Payload contract (defined here, the minimum FR-8 needs):
`{"payload": {"productId": "..."}}`.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import InventoryError
from domain.inventory_stock_service import InventoryStockService

logger = logging.getLogger(__name__)


class CatalogUpdatedConsumer:
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
                "catalog_updated_consumer.receive_message failed",
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
            product_id = body["payload"]["productId"]
            if not isinstance(product_id, str) or not product_id:
                raise TypeError(f"productId must be a non-empty string, got {product_id!r}")
            # Idempotent — FR-8: a redelivered CatalogUpdated for an
            # already-provisioned productId is a no-op.
            self._stock_service.provision_product(product_id)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "catalog_updated_consumer failed to process message — left for retry/DLQ",
                extra={
                    "correlationId": correlation_id,
                    "messageId": message.get("MessageId"),
                    "error": str(exc),
                },
            )
            return
        except InventoryError as exc:
            logger.error(
                "catalog_updated_consumer failed to provision stock row — left for retry/DLQ",
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
                "catalog_updated_consumer.delete_message failed",
                extra={"correlationId": correlation_id, "error": str(exc)},
            )
