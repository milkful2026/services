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
        if detail_type == "UserRegistered":
            # User Service publishes this one through its own, older
            # adapters/outbox_event_publisher.py (predates shared/'s), which
            # wraps the actual domain payload one level deeper than every
            # other event here does:
            # {eventId, eventType, eventVersion, source, timestamp,
            #  correlationId, payload: {...}} — vs. PaymentConfirmed etc.
            # (published via shared.adapters.outbox_event_publisher),
            # which puts the payload's own fields directly on `detail`.
            # Confirmed by inspecting a real message on wallet-events-q's
            # DLQ (MA-134) — `detail["userId"]` alone always KeyErrored,
            # since the real key only ever existed at `detail["payload"]
            # ["userId"]`.
            self._wallet_service.create_wallet(detail.get("payload", detail))
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
