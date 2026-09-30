"""SQS consumer for `subscription-events-q` (MA-140).

Consumes `user.status.changed` (MA-139's contract, owned there — this
consumer treats it as fixed, never redefines it). Mirrors wallet's own
`adapters/wallet_events_consumer.py` shape exactly (poll -> dispatch by
EventBridge `detail-type` -> domain call -> delete-on-success), since
this service had no SQS-consuming handler of its own to copy from
before this.

A message whose processing raises (a DB hiccup pausing one of the
user's subscriptions, a malformed payload) is NOT deleted — SQS
redelivers it, and after the redrive count it lands in the DLQ with an
alarm (spec section 5 NFR-Reliability). Per spec FR-1 step 3, the
message is only deleted once every subscription for that user has been
paused successfully; a partial failure leaves the whole message for
redrive rather than acking a half-applied batch.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import SubscriptionError
from domain.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)

try:  # optional in prod images; present in dev for contract validation
    import jsonschema
    from shared.events import load_schema  # type: ignore

    _USER_STATUS_CHANGED_SCHEMA = load_schema("UserStatusChanged")
    _SCHEMA_VALIDATION_ERROR: type[Exception] | tuple[()] = jsonschema.ValidationError
except Exception:  # noqa: BLE001 — same optional-dependency posture as wallet's consumer
    jsonschema = None
    _USER_STATUS_CHANGED_SCHEMA = None
    _SCHEMA_VALIDATION_ERROR = ()


def _unwrap_envelope(detail: dict) -> dict:
    """User Service's `user.status.changed` is published via its own
    (pre-`shared/`) adapters/outbox_event_publisher.py, whose envelope
    nests the actual contract fields one level deeper, under `payload`
    — {eventId, eventType, eventVersion, source, timestamp,
    correlationId, payload: {...}} — instead of putting them flat on
    `detail` the way shared/adapters/outbox_event_publisher.py's
    producers do. Same shape, same fix, as wallet_events_consumer.py's
    own `_unwrap_legacy_envelope` (confirmed against a real UserRegistered
    message there, MA-134) — applied here for the same producer."""
    payload = detail.get("payload")
    return payload if isinstance(payload, dict) else detail


class UserStatusChangedConsumer:
    def __init__(
        self,
        queue_url: str,
        subscription_service: SubscriptionService,
        region_name: str,
    ) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._subscription_service = subscription_service

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error(
                "user_status_changed_consumer.receive_message failed", extra={"error": str(exc)}
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
            envelope = json.loads(message["Body"])
            detail_type = envelope.get("detail-type") or envelope.get("detailType")
            detail = envelope.get("detail", {})
            self._dispatch(detail_type, detail)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "user_status_changed_consumer: malformed message — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except SubscriptionError as exc:
            logger.error(
                "user_status_changed_consumer: transient processing failure — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except _SCHEMA_VALIDATION_ERROR as exc:
            logger.error(
                "user_status_changed_consumer: schema-invalid UserStatusChanged — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return

        try:
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        except ClientError as exc:
            logger.error(
                "user_status_changed_consumer.delete_message failed", extra={"error": str(exc)}
            )

    def _dispatch(self, detail_type: str | None, detail: dict) -> None:
        if detail_type != "user.status.changed":
            logger.info("user_status_changed_consumer: unhandled", extra={"dt": detail_type})
            return

        payload = _unwrap_envelope(detail)
        if jsonschema is not None and _USER_STATUS_CHANGED_SCHEMA is not None:
            jsonschema.validate(payload, _USER_STATUS_CHANGED_SCHEMA)

        paused_ids = self._subscription_service.handle_user_status_changed(payload)
        if paused_ids:
            logger.info(
                "user_status_changed_consumer: paused subscriptions for account status change",
                extra={
                    "metric": "subscription.admin_pause.count",
                    "userId": payload.get("userId"),
                    "newStatus": payload.get("newStatus"),
                    "count": len(paused_ids),
                },
            )
