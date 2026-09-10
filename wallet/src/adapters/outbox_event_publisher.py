"""EventBridge publisher used only by the outbox publisher loop — never
called from the request-handling or SQS-consumer path (transactional
outbox pattern). Same mechanism as services/cart's own
outbox_event_publisher.py.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from adapters.retry import call_with_retry
from domain.exceptions import ServiceUnavailableError

logger = logging.getLogger(__name__)


class _RetryablePublishError(Exception):
    pass


class EventBridgeOutboxPublisher:
    def __init__(
        self,
        event_bus_name: str,
        event_source: str,
        region_name: str,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self._client = boto3.client("events", region_name=region_name)
        self._event_bus_name = event_bus_name
        self._event_source = event_source
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    def publish(self, event_type: str, detail: dict) -> None:
        # `detail` is already the full contract payload (built in the
        # domain when the outbox row was written). Only stamp an envelope
        # id/time if missing.
        detail.setdefault("eventId", str(uuid.uuid4()))
        detail.setdefault("occurredAt", datetime.now(UTC).isoformat())

        def _attempt() -> None:
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
            except ClientError as exc:
                raise _RetryablePublishError(str(exc)) from exc
            if response.get("FailedEntryCount", 0) != 0:
                raise _RetryablePublishError(str(response.get("Entries")))

        def _on_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "outbox_event_publisher.put_events failed",
                extra={"attempt": attempt, "error": str(exc)},
            )

        try:
            call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryablePublishError,),
                on_attempt_failure=_on_failure,
            )
        except _RetryablePublishError as exc:
            raise ServiceUnavailableError(
                "Failed to publish event after retries", details={"cause": str(exc)}
            ) from exc
