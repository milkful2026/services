"""SQS consumer for `wallet-events-q`.

Multiplexes by EventBridge `detail-type`:
  - UserRegistered                         -> WalletService.create_wallet   (MA-1 baseline)
  - PaymentConfirmed & purpose=WALLET_RECHARGE -> WalletService.credit_recharge (MA-24)
  - OrderConfirmed / OrderCancelled        -> ack + log "unhandled (MA-97)"  (future)
  - anything else                          -> ack + log "unhandled"

A message that raises RetryableConsumerError (e.g. a recharge that beat
the wallet's provisioning) is NOT deleted — SQS redelivers it, and after
the redrive count it lands in the DLQ with an alarm. It is never
acked-and-dropped.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import RetryableConsumerError, WalletError
from domain.wallet_service import WalletService

logger = logging.getLogger(__name__)

try:  # optional in prod images; present in dev for contract validation
    import jsonschema
    from shared.events import load_schema  # type: ignore

    _PAYMENT_CONFIRMED_SCHEMA = load_schema("PaymentConfirmed")
    # An empty tuple as the "no jsonschema" fallback below means the except
    # clause that names this never matches anything (rather than raising
    # AttributeError trying to look up .ValidationError on None the first
    # time some *other* exception is being matched against it).
    _SCHEMA_VALIDATION_ERROR: type[Exception] | tuple[()] = jsonschema.ValidationError
except Exception:  # noqa: BLE001
    jsonschema = None
    _PAYMENT_CONFIRMED_SCHEMA = None
    _SCHEMA_VALIDATION_ERROR = ()


class WalletEventsConsumer:
    def __init__(
        self,
        queue_url: str,
        wallet_service: WalletService,
        region_name: str,
    ) -> None:
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url
        self._wallet_service = wallet_service

    def poll_once(self, max_messages: int = 10, wait_time_seconds: int = 10) -> int:
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
            )
        except ClientError as exc:
            logger.error("wallet_events_consumer.receive_message failed", extra={"error": str(exc)})
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
        except RetryableConsumerError as exc:
            logger.warning(
                "wallet_events_consumer: retryable — leaving message for redelivery/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return  # do NOT delete
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "wallet_events_consumer: malformed message — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except WalletError as exc:
            logger.error(
                "wallet_events_consumer: transient processing failure — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return
        except _SCHEMA_VALIDATION_ERROR as exc:
            logger.error(
                "wallet_events_consumer: schema-invalid PaymentConfirmed — left for retry/DLQ",
                extra={"messageId": message.get("MessageId"), "error": str(exc)},
            )
            return

        try:
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        except ClientError as exc:
            logger.error("wallet_events_consumer.delete_message failed", extra={"error": str(exc)})

    def _dispatch(self, detail_type: str | None, detail: dict) -> None:
        detail = _unwrap_legacy_envelope(detail)
        if detail_type == "UserRegistered":
            self._wallet_service.create_wallet(detail)
            return
        if detail_type == "PaymentConfirmed" and detail.get("purpose") == "WALLET_RECHARGE":
            if jsonschema is not None and _PAYMENT_CONFIRMED_SCHEMA is not None:
                jsonschema.validate(detail, _PAYMENT_CONFIRMED_SCHEMA)
            self._wallet_service.credit_recharge(detail)
            return
        if detail_type in ("OrderConfirmed", "OrderCancelled"):
            logger.info("wallet_events_consumer: unhandled (MA-97)", extra={"dt": detail_type})
            return
        logger.info("wallet_events_consumer: unhandled", extra={"dt": detail_type})


def _unwrap_legacy_envelope(detail: dict) -> dict:
    """Some producers wrap the actual domain payload one level deeper
    than every event published via shared.adapters.outbox_event_publisher
    does. User Service's own adapters/outbox_event_publisher.py (predates
    shared/'s) is one — Cart's own copy has the identical shape:
    {eventId, eventType, eventVersion, source, timestamp, correlationId,
     payload: {...}} instead of putting the payload's own fields directly
    on `detail`. Confirmed by inspecting a real UserRegistered message on
    wallet-events-q's DLQ (MA-134) — `detail["userId"]` alone always
    KeyErrored, since the real key only ever existed at
    `detail["payload"]["userId"]`.

    Applied generically to every detail_type here, not special-cased to
    UserRegistered, so the next event type this consumer subscribes to
    from a legacy-shaped publisher doesn't reintroduce the same KeyError.
    Safe to key off `payload` alone: every flat-shape event schema in
    this codebase declares `additionalProperties: false` and none uses a
    top-level field literally named `payload`, so its presence is an
    unambiguous "legacy envelope" signal, never a real domain field."""
    payload = detail.get("payload")
    return payload if isinstance(payload, dict) else detail
