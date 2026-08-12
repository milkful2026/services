"""SQS consumer for `ZoneUpdated` (FR-3) — busts cached serviceability
results for the updated zone's pincode prefixes.

Event payload contract (not specified further by the spec, defined here):
`{"payload": {"zoneId": "...", "pincodePrefixes": ["5600", ...]}}`, per
the domain event envelope in services/README.md §5. A prefix-level bust
(not per-pincode) is used because the cache only knows which exact
pincodes were queried, not which ones fall under a changed zone.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from adapters.interfaces import ZoneCachePort

logger = logging.getLogger(__name__)


class ZoneUpdateConsumer:
    def __init__(
        self,
        queue_url: str,
        zone_cache: ZoneCachePort,
        region_name: str,
        correlation_id: str = "",
    ) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._zone_cache = zone_cache
        self._correlation_id = correlation_id

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        """Receives and processes up to `max_messages`. Returns the count
        received (not necessarily all successfully processed — malformed
        messages are left in-queue to retry, then DLQ per the queue's
        redrive policy, per services/README.md §5's "failed messages:
        retries then DLQ" convention)."""
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error(
                "zone_update_consumer.receive_message failed",
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
        try:
            body = json.loads(message["Body"])
            prefixes = body["payload"]["pincodePrefixes"]
            for prefix in prefixes:
                self._zone_cache.invalidate_by_prefix(prefix)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "zone_update_consumer failed to process message — left for retry/DLQ",
                extra={
                    "correlationId": self._correlation_id,
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
                "zone_update_consumer.delete_message failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
