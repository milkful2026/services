"""EventBridge publisher used only by outbox_publisher_handler — never
called from the request-handling path (spec §6 outbox pattern). Publishes
to the account default bus, same reasoning as MA-92/MA-95's own adapters.
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)


class EventBridgeOutboxPublisher:
    def __init__(
        self,
        event_bus_name: str,
        event_source: str,
        region_name: str,
        correlation_id: str = "",
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self._client = boto3.client("events", region_name=region_name)
        self._event_bus_name = event_bus_name
        self._event_source = event_source
        self._correlation_id = correlation_id
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    def publish(self, event_type: str, payload: dict, correlation_id: str) -> None:
        detail = {
            "eventId": str(uuid.uuid4()),
            "eventType": event_type,
            "eventVersion": "1.0",
            "source": self._event_source,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id,
            "payload": payload,
        }

        last_cause: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.put_events(
                    Entries=[
                        {
                            "Source": self._event_source,
                            "DetailType": event_type,
                            "Detail": json.dumps(detail),
                            "EventBusName": self._event_bus_name,
                        }
                    ]
                )
                if response.get("FailedEntryCount", 0) == 0:
                    return
                last_cause = str(response.get("Entries"))
                logger.error(
                    "outbox_event_publisher.put_events entry failed",
                    extra={"correlationId": self._correlation_id, "entries": response.get("Entries")},
                )
            except ClientError as exc:
                last_cause = str(exc)
                logger.error(
                    "outbox_event_publisher.put_events failed",
                    extra={"correlationId": self._correlation_id, "attempt": attempt, "error": last_cause},
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_base_seconds * (2**attempt))

        raise ExternalServiceUnavailableError(
            "Failed to publish event after retries", details={"cause": last_cause}
        )
