"""EventBridge publisher for the OtpRequested domain event.

Per services/README.md §5: publishes to the account default bus (no
shared "milkful-domain-events" bus exists yet — creating/naming one is
an architecture decision flagged for the architect, not decided here).
Retries with backoff per §3.7's external-adapter pattern; the caller
(otp_send_handler) decides whether a publish failure should surface to
the client (spec FR-1 edge cases: SMS failure -> retry 2x, surface error).
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import NotificationPublishError

logger = logging.getLogger(__name__)


class EventBridgeNotificationPublisher:
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

    def publish_otp_requested(self, mobile: str, otp: str, correlation_id: str) -> None:
        detail = {
            "eventId": str(uuid.uuid4()),
            "eventType": "identity.otp.requested",
            "eventVersion": "1.0",
            "source": self._event_source,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id,
            "payload": {"mobile": mobile, "otp": otp},
        }

        last_cause: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.put_events(
                    Entries=[
                        {
                            "Source": self._event_source,
                            "DetailType": "identity.otp.requested",
                            "Detail": json.dumps(detail),
                            "EventBusName": self._event_bus_name,
                        }
                    ]
                )
                if response.get("FailedEntryCount", 0) == 0:
                    return
                last_cause = str(response.get("Entries"))
                logger.error(
                    "notification_publisher.put_events entry failed",
                    extra={
                        "correlationId": self._correlation_id,
                        "entries": response.get("Entries"),
                    },
                )
            except ClientError as exc:
                last_cause = str(exc)
                logger.error(
                    "notification_publisher.put_events failed",
                    extra={
                        "correlationId": self._correlation_id,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_base_seconds * (2**attempt))

        raise NotificationPublishError(
            "Failed to publish OtpRequested after retries", details={"cause": last_cause}
        )
