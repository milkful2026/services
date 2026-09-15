"""Payment domain service — the only place business rules live.

Scoped to `purpose = WALLET_RECHARGE` (MA-126 §3). The webhook is
authoritative for CONFIRMED/FAILED; the client's own `confirm` call only
ever advances a payment to CONFIRMING.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from adapters.interfaces import PaymentGatewayPort, PaymentRepositoryPort, WalletLimitsPort
from config.env import Settings
from domain.exceptions import (
    AmountOutOfRangeError,
    IdempotencyKeyReusedError,
    OrderMismatchError,
    PaymentNotFoundError,
    SignatureInvalidError,
    UnsupportedPurposeError,
)
from domain.metrics import MetricsPort
from domain.models import Payment, PaymentStatus, Purpose

logger = logging.getLogger(__name__)


class PaymentService:
    def __init__(
        self,
        repository: PaymentRepositoryPort,
        gateway: PaymentGatewayPort,
        wallet_limits: WalletLimitsPort,
        metrics: MetricsPort,
        settings: Settings,
    ) -> None:
        self._repo = repository
        self._gateway = gateway
        self._wallet_limits = wallet_limits
        self._metrics = metrics
        self._settings = settings

    # --- FR-1: create ---

    def create(
        self,
        *,
        user_id: str,
        purpose: str,
        amount_paise: int,
        currency: str,
        method: str | None,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict:
        existing = self._repo.get_by_user_idem(user_id, idempotency_key)
        if existing is not None:
            if (
                existing.purpose.value != purpose
                or existing.amount_paise != amount_paise
                or existing.currency != currency
            ):
                raise IdempotencyKeyReusedError(
                    "This idempotency key was already used for a different request",
                    details={
                        "amountPaise": existing.amount_paise,
                        "purpose": existing.purpose.value,
                        "currency": existing.currency,
                    },
                )
            if existing.razorpay_order_id:
                # Verbatim replay — never a second Razorpay order for the
                # same attempt (PR #16 round-2 finding #1).
                return self._create_response(existing)
            # A prior attempt inserted the row but orders.create failed
            # (GatewayUnavailableError) before an order_id was persisted.
            # Resume, don't re-validate — this is a retry of the same
            # attempt, not a new one.
            order_id = self._gateway.orders_create(
                amount_paise=existing.amount_paise,
                receipt=existing.id,
                notes={"userId": user_id, "purpose": existing.purpose.value},
            )
            self._repo.set_order_id(existing.id, order_id)
            self._repo.append_event(
                existing.id, "INTERNAL_CREATE", {"razorpayOrderId": order_id, "resumed": True}
            )
            existing.razorpay_order_id = order_id
            return self._create_response(existing)

        if purpose != Purpose.WALLET_RECHARGE.value:
            raise UnsupportedPurposeError(f"Unsupported purpose: {purpose!r}")

        min_paise, max_paise = self._wallet_limits.get_limits()
        if not (min_paise <= amount_paise <= max_paise):
            raise AmountOutOfRangeError(
                "Amount is outside the allowed recharge range",
                details={"rechargeMinPaise": min_paise, "rechargeMaxPaise": max_paise},
            )

        payment_id = _new_payment_id()
        payment = self._repo.insert_created(
            payment_id=payment_id,
            user_id=user_id,
            purpose=purpose,
            amount_paise=amount_paise,
            currency=currency,
            method=method,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        order_id = self._gateway.orders_create(
            amount_paise=amount_paise,
            receipt=payment_id,
            notes={"userId": user_id, "purpose": purpose},
        )
        self._repo.set_order_id(payment_id, order_id)
        self._repo.append_event(payment_id, "INTERNAL_CREATE", {"razorpayOrderId": order_id})
        payment.razorpay_order_id = order_id
        self._metrics.emit("recharge.created")
        return self._create_response(payment)

    def _create_response(self, payment: Payment) -> dict:
        return {
            "paymentId": payment.id,
            "razorpayOrderId": payment.razorpay_order_id,
            "amountPaise": payment.amount_paise,
            "currency": payment.currency,
            "status": payment.status.value,
        }

    # --- FR-2: client confirm ---

    def confirm(
        self,
        *,
        payment_id: str,
        user_id: str,
        razorpay_payment_id: str,
        razorpay_order_id: str,
        razorpay_signature: str,
    ) -> dict:
        payment = self._repo.lock_by_id(payment_id)
        if payment is None or payment.user_id != user_id:
            raise PaymentNotFoundError("No such payment")

        if not self._gateway.verify_client_signature(
            razorpay_order_id, razorpay_payment_id, razorpay_signature
        ):
            self._repo.append_event(
                payment_id,
                "CLIENT_CONFIRM_REJECTED",
                {"razorpayOrderId": razorpay_order_id, "razorpayPaymentId": razorpay_payment_id},
            )
            raise SignatureInvalidError("Payment signature could not be verified")

        if payment.razorpay_order_id != razorpay_order_id:
            raise OrderMismatchError("razorpayOrderId does not match this payment")

        if payment.status in (PaymentStatus.CONFIRMED, PaymentStatus.FAILED):
            return self._get_response(payment)
        if payment.status == PaymentStatus.CONFIRMING:
            return self._get_response(payment)

        self._repo.set_status(
            payment_id,
            status=PaymentStatus.CONFIRMING.value,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        self._repo.append_event(
            payment_id,
            "CLIENT_CONFIRM",
            {"razorpayOrderId": razorpay_order_id, "razorpayPaymentId": razorpay_payment_id},
        )
        self._metrics.emit("recharge.confirming")
        payment.status = PaymentStatus.CONFIRMING
        return self._get_response(payment)

    # --- FR-3: webhook (authoritative) ---

    def apply_webhook(self, raw_body: bytes, signature_header: str) -> dict:
        if not self._gateway.verify_webhook_signature(raw_body, signature_header):
            self._metrics.emit("webhook.signature_invalid")
            raise SignatureInvalidError("Webhook signature could not be verified")

        body = json.loads(raw_body.decode("utf-8"))
        event = body.get("event", "")
        self._metrics.emit("webhook.received", event=event)

        if event in ("payment.captured", "order.paid"):
            entity = body["payload"]["payment"]["entity"]
            self._apply_captured(entity, source="WEBHOOK")
        elif event == "payment.failed":
            entity = body["payload"]["payment"]["entity"]
            self._apply_failed(entity, source="WEBHOOK")
        else:
            logger.info("apply_webhook: unhandled event", extra={"event": event})
        return {"status": "ok"}

    def _apply_captured(self, entity: dict, *, source: str) -> None:
        order_id = entity["order_id"]
        rzp_payment_id = entity["id"]
        amount_paise = int(entity["amount"])
        method = (entity.get("method") or "OTHER").upper()

        row = self._repo.lock_by_order_id(order_id)
        if row is None:
            logger.warning(
                "apply_webhook: orphan captured event, no matching payment",
                extra={"razorpayOrderId": order_id},
            )
            self._repo.append_event("unknown", "ORPHAN_WEBHOOK", entity)
            return

        if row.status == PaymentStatus.CONFIRMED:
            self._repo.append_event(row.id, "WEBHOOK_DUP", entity)
            return

        provisional_timeout = row.status == PaymentStatus.FAILED and row.failure_code == "TIMEOUT"
        if row.status == PaymentStatus.FAILED and not provisional_timeout:
            # A real Razorpay-reported failure is terminal. A late capture
            # arriving after that is not auto-recovered — flag for manual
            # review (PR #16 round-2 finding #7).
            self._repo.append_event(row.id, "WEBHOOK_DUP", entity)
            logger.error(
                "late_capture_after_fail",
                extra={"paymentId": row.id, "razorpayOrderId": order_id},
            )
            return

        if row.amount_paise != amount_paise:
            self._repo.set_status(
                row.id, status=PaymentStatus.FAILED.value, failure_code="AMOUNT_MISMATCH"
            )
            self._repo.append_event(row.id, source, entity)
            self._enqueue_payment_failed(
                row,
                failure_code="AMOUNT_MISMATCH",
                failure_reason="Captured amount did not match",
            )
            self._metrics.emit("recharge.failed", code="AMOUNT_MISMATCH")
            logger.error(
                "apply_webhook: amount mismatch",
                extra={"paymentId": row.id, "expected": row.amount_paise, "actual": amount_paise},
            )
            return

        self._repo.set_status(
            row.id,
            status=PaymentStatus.CONFIRMED.value,
            method=method,
            razorpay_payment_id=rzp_payment_id,
            captured_at_now=True,
        )
        self._repo.append_event(
            row.id, "LATE_CAPTURE_RECOVERED" if provisional_timeout else source, entity
        )
        self._enqueue_payment_confirmed(
            row, amount_paise=amount_paise, method=method, rzp_payment_id=rzp_payment_id
        )
        self._metrics.emit("recharge.confirmed")

    def _apply_failed(self, entity: dict, *, source: str) -> None:
        order_id = entity["order_id"]
        row = self._repo.lock_by_order_id(order_id)
        if row is None:
            logger.warning(
                "apply_webhook: orphan failed event, no matching payment",
                extra={"razorpayOrderId": order_id},
            )
            return
        if row.status in (PaymentStatus.CONFIRMED, PaymentStatus.FAILED):
            self._repo.append_event(row.id, "WEBHOOK_DUP", entity)
            return

        failure_code = entity.get("error_code") or "PAYMENT_FAILED"
        failure_reason = entity.get("error_description") or "Payment failed"
        self._repo.set_status(
            row.id,
            status=PaymentStatus.FAILED.value,
            failure_code=failure_code,
            failure_reason=failure_reason,
        )
        self._repo.append_event(row.id, source, entity)
        self._enqueue_payment_failed(row, failure_code=failure_code, failure_reason=failure_reason)
        self._metrics.emit("recharge.failed", code=failure_code)

    # --- FR-4: read ---

    def get(self, payment_id: str, user_id: str) -> dict:
        payment = self._repo.get(payment_id)
        if payment is None or payment.user_id != user_id:
            raise PaymentNotFoundError("No such payment")
        return self._get_response(payment)

    def _get_response(self, payment: Payment) -> dict:
        return {
            "paymentId": payment.id,
            "purpose": payment.purpose.value,
            "status": payment.status.value,
            "amountPaise": payment.amount_paise,
            "currency": payment.currency,
            "method": payment.method.value if payment.method else None,
            "razorpayOrderId": payment.razorpay_order_id,
            "razorpayPaymentId": payment.razorpay_payment_id,
            "failureReason": payment.failure_reason,
        }

    # --- FR-5: reconciliation sweep ---

    def reconcile_once(self) -> list[str]:
        candidates = self._repo.list_stale(
            (PaymentStatus.CONFIRMING.value, PaymentStatus.CREATED.value),
            self._settings.reconcile_stale_seconds,
        )
        outcomes: list[str] = []
        now = datetime.now(UTC)
        for row in candidates:
            if row.status == PaymentStatus.CREATED and not row.razorpay_order_id:
                # Never reached Razorpay — nothing to reconcile against;
                # the client retries with the same idempotency key.
                continue
            outcomes.append(self._reconcile_one(row, now))
        return outcomes

    def _reconcile_one(self, row: Payment, now: datetime) -> str:
        payments = self._gateway.fetch_order_payments(row.razorpay_order_id)
        captured = next((p for p in payments if p.get("status") == "captured"), None)
        if captured is not None:
            self._apply_captured(captured, source="RECONCILE")
            self._metrics.emit("recharge.reconciled", outcome="confirmed")
            return "confirmed"

        all_failed = bool(payments) and all(p.get("status") == "failed" for p in payments)
        if all_failed:
            self._apply_failed(payments[-1], source="RECONCILE")
            self._metrics.emit("recharge.reconciled", outcome="failed")
            return "failed"

        updated_at = row.updated_at or now
        age_seconds = (now - _as_aware_utc(updated_at)).total_seconds()
        if age_seconds >= self._settings.reconcile_hard_cap_seconds:
            self._repo.set_status(
                row.id, status=PaymentStatus.FAILED.value, failure_code="TIMEOUT"
            )
            self._repo.append_event(row.id, "RECONCILE", {"reason": "hard_cap_exceeded"})
            self._enqueue_payment_failed(
                row, failure_code="TIMEOUT", failure_reason="Payment timed out"
            )
            self._metrics.emit("recharge.failed", code="TIMEOUT")
            self._metrics.emit("recharge.reconciled", outcome="timeout")
            return "timeout"

        if age_seconds >= self._settings.reconcile_confirming_alert_seconds:
            self._metrics.emit("payment.confirming_over_30m", paymentId=row.id)
            self._metrics.emit("recharge.reconciled", outcome="still_confirming_alert")
            return "still_confirming_alert"

        self._metrics.emit("recharge.reconciled", outcome="still_confirming")
        return "still_confirming"

    # --- outbox payload builders ---

    def _enqueue_payment_confirmed(
        self, payment: Payment, *, amount_paise: int, method: str, rzp_payment_id: str
    ) -> None:
        detail = {
            "eventId": str(uuid.uuid4()),
            "occurredAt": datetime.now(UTC).isoformat(),
            "correlationId": payment.correlation_id,
            "paymentId": payment.id,
            "userId": payment.user_id,
            "purpose": payment.purpose.value,
            "amountPaise": amount_paise,
            "currency": payment.currency,
            "method": method,
            "razorpayPaymentId": rzp_payment_id,
            "razorpayOrderId": payment.razorpay_order_id,
        }
        self._repo.enqueue_outbox(payment.id, "PaymentConfirmed", detail)

    def _enqueue_payment_failed(
        self, payment: Payment, *, failure_code: str, failure_reason: str
    ) -> None:
        detail = {
            "eventId": str(uuid.uuid4()),
            "occurredAt": datetime.now(UTC).isoformat(),
            "correlationId": payment.correlation_id,
            "paymentId": payment.id,
            "userId": payment.user_id,
            "purpose": payment.purpose.value,
            "amountPaise": payment.amount_paise,
            "currency": payment.currency,
            "razorpayPaymentId": payment.razorpay_payment_id,
            "razorpayOrderId": payment.razorpay_order_id,
            "failureCode": failure_code,
            "failureReason": failure_reason,
        }
        self._repo.enqueue_outbox(payment.id, "PaymentFailed", detail)


def _new_payment_id() -> str:
    # Kept in the domain (not the repository) so the id format is a
    # business decision, not a storage detail — services/README.md §3.4's
    # dependency-direction rule: domain never imports an adapter.
    return f"pay_{uuid.uuid4().hex}"


def _as_aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
