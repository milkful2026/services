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
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from adapters.retry import call_with_retry
from domain.exceptions import NotificationPublishError

logger = logging.getLogger(__name__)


class _PutEventsEntryFailed(Exception):
    """Internal signal for a soft EventBridge PutEvents failure
    (FailedEntryCount != 0 with no raised ClientError) — raised so
    call_with_retry's exception-based retry loop can treat this the
    same as a raised ClientError, instead of needing its own
    hand-rolled retry loop to inspect a return value."""


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

    def publish_otp_requested(
        self, mobile: str, otp: str, correlation_id: str, purpose: str = "REGISTER"
    ) -> None:
        # template mirrors OtpRecord.purpose (register vs login) so
        # Notification can pick the right SMS copy — spec MA-21 FR-1.
        template = "login" if purpose == "LOGIN" else "registration"
        detail = {
            "eventId": str(uuid.uuid4()),
            "eventType": "identity.otp.requested",
            "eventVersion": "1.0",
            "source": self._event_source,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id,
            "payload": {"mobile": mobile, "otp": otp, "template": template},
        }
        self._put_with_retry(
            "identity.otp.requested", detail, failure_message="Failed to publish OtpRequested after retries"
        )

    def publish_admin_event(self, event_type: str, payload: dict, correlation_id: str) -> None:
        """Publishes one of the `admin.*` domain events (MA-129 FR-6)
        using the same standard envelope and retry/backoff behavior as
        publish_otp_requested — reused per spec §6's explicit instruction
        rather than a second EventBridge adapter."""
        detail = {
            "eventId": str(uuid.uuid4()),
            "eventType": event_type,
            "eventVersion": "1.0",
            "source": self._event_source,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id,
            "payload": payload,
        }
        self._put_with_retry(
            event_type, detail, failure_message=f"Failed to publish {event_type} after retries"
        )

    def _put_with_retry(self, detail_type: str, detail: dict, failure_message: str) -> None:
        def _attempt():
            response = self._client.put_events(
                Entries=[
                    {
                        "Source": self._event_source,
                        "DetailType": detail_type,
                        "Detail": json.dumps(detail),
                        "EventBusName": self._event_bus_name,
                    }
                ]
            )
            if response.get("FailedEntryCount", 0) != 0:
                logger.error(
                    "notification_publisher.put_events entry failed",
                    extra={"correlationId": self._correlation_id, "entries": response.get("Entries")},
                )
                raise _PutEventsEntryFailed(str(response.get("Entries")))

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            if isinstance(exc, ClientError):
                logger.error(
                    "notification_publisher.put_events failed",
                    extra={"correlationId": self._correlation_id, "attempt": attempt, "error": str(exc)},
                )

        try:
            call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(ClientError, _PutEventsEntryFailed),
                on_attempt_failure=_on_attempt_failure,
            )
        except (ClientError, _PutEventsEntryFailed) as exc:
            raise NotificationPublishError(failure_message, details={"cause": str(exc)}) from exc
