"""MA-143 reconciliation sweep: finishes or safely closes records a crash or
a dependency outage left half-done.

Subscription orders stuck CREATED (MA-143 FR-4): charged once through the
same debit step `materialize`'s crash-resume uses, or — once the delivery
can no longer be scheduled — escalated to NEEDS_ATTENTION without
charging.

Checkouts abandoned IN_PROGRESS (MA-144): resumed through the same steps
a customer retry runs; cancelled without charging when nothing was
charged and the delivery date has passed its cut-off (PD-1); completed
with the unstarted subscription lines left in the cart when the budget
runs out after payment (PD-2); otherwise escalated.

Every record is worked under a lease (see the repository), so the sweep,
SQS redelivery and several Order tasks never run the same record at once.
Failed attempts are counted; at the budget the record is escalated
rather than retried for ever.
"""

import logging
from collections import Counter
from datetime import datetime

from domain.checkout_models import CheckoutStatus, CheckoutStep
from domain.cutoff import delivery_cutoff_passed
from domain.exceptions import (
    CheckoutIncompleteError,
    InsufficientBalanceError,
    LeaseLostError,
    OrderError,
    WalletNotActiveError,
    WalletUnavailableError,
)
from domain.models import FAILURE_CUTOFF_PASSED, FAILURE_SWEEP_EXHAUSTED, OrderStatus

logger = logging.getLogger(__name__)

_ORDER_FLOW = "sweep.subscription_order"
_CHECKOUT_FLOW = "sweep.checkout"


class SweepService:
    def __init__(
        self,
        repository,
        order_service,
        metrics,
        *,
        owner: str,
        cutoff_hour_ist: int,
        subscription_order_stale_seconds: float,
        max_attempts: int,
        lease_seconds: float,
        batch_size: int,
        checkout_service=None,
        checkout_stale_seconds: float = 600,
    ) -> None:
        self._repo = repository
        self._order_service = order_service
        self._metrics = metrics
        self._owner = owner
        self._cutoff_hour_ist = cutoff_hour_ist
        self._order_stale_seconds = subscription_order_stale_seconds
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._checkout_service = checkout_service
        self._checkout_stale_seconds = checkout_stale_seconds

    # --- FR-4: stuck subscription orders ---

    def sweep_subscription_orders(self, correlation_id: str, now: datetime) -> Counter:
        counts: Counter = Counter()
        order_ids = self._repo.list_stale_subscription_orders(
            self._order_stale_seconds, self._batch_size
        )
        for order_id in order_ids:
            counts["found"] += 1
            self._metrics.emit(f"{_ORDER_FLOW}.found")
            try:
                outcome = self._sweep_order(order_id, correlation_id, now)
            except Exception as exc:  # noqa: BLE001 — one record never stops the run
                logger.exception(
                    "sweep.subscription_order: unexpected error",
                    extra={"orderId": order_id, "correlationId": correlation_id},
                )
                outcome = self._record_order_failure(
                    order_id, f"UNEXPECTED:{type(exc).__name__}", correlation_id
                )
            if outcome:
                counts[outcome] += 1
        return counts

    def _sweep_order(self, order_id: str, correlation_id: str, now: datetime) -> str | None:
        if not self._repo.claim_order(order_id, self._owner, self._lease_seconds):
            return None  # another worker has it
        order = self._repo.get(order_id)
        if order is None or order.status != OrderStatus.CREATED:
            self._repo.release_order(order_id, self._owner)
            return None
        attempt = order.sweep_attempts + 1

        if delivery_cutoff_passed(order.delivery_date, now, self._cutoff_hour_ist):
            # D-5: too late to deliver — never charge it.
            self._repo.escalate_order(order.id, self._owner, FAILURE_CUTOFF_PASSED)
            self._log(order.id, attempt, "escalated", correlation_id, FAILURE_CUTOFF_PASSED)
            self._metrics.emit(f"{_ORDER_FLOW}.escalated", reason=FAILURE_CUTOFF_PASSED)
            return "escalated"

        self._metrics.emit(f"{_ORDER_FLOW}.resumed")
        try:
            # Charges once, or replays a debit the crashed attempt already made.
            self._order_service.resume_debit(order, correlation_id)
        except WalletUnavailableError as exc:
            return self._record_order_failure(order.id, exc.error_code, correlation_id)

        after = self._repo.get(order.id)
        outcome = "confirmed" if after.status == OrderStatus.CONFIRMED else "payment_failed"
        self._log(order.id, attempt, outcome, correlation_id)
        self._metrics.emit(f"{_ORDER_FLOW}.{outcome}")
        return outcome

    def _record_order_failure(self, order_id: str, error_code: str, correlation_id: str) -> str:
        self._metrics.emit(f"{_ORDER_FLOW}.failed_attempt", error=error_code)
        try:
            escalated = self._repo.record_order_sweep_failure(
                order_id, self._owner, error_code, self._max_attempts
            )
        except OrderError:
            # DB trouble too — the lease simply expires and a later run retries.
            logger.exception(
                "sweep.subscription_order: could not record the failed attempt",
                extra={"orderId": order_id, "correlationId": correlation_id},
            )
            return "failed_attempt"
        if escalated:
            self._log(order_id, None, "escalated", correlation_id, FAILURE_SWEEP_EXHAUSTED)
            self._metrics.emit(f"{_ORDER_FLOW}.escalated", reason=FAILURE_SWEEP_EXHAUSTED)
            return "escalated"
        self._log(order_id, None, "failed_attempt", correlation_id, error_code)
        return "failed_attempt"

    # --- MA-144: abandoned checkouts ---

    def sweep_checkouts(self, correlation_id: str, now: datetime) -> Counter:
        counts: Counter = Counter()
        if self._checkout_service is None:
            return counts
        checkout_ids = self._repo.list_stale_checkouts(
            self._checkout_stale_seconds, self._batch_size
        )
        for checkout_id in checkout_ids:
            counts["found"] += 1
            self._metrics.emit(f"{_CHECKOUT_FLOW}.found")
            try:
                outcome = self._sweep_checkout(checkout_id, correlation_id, now)
            except Exception as exc:  # noqa: BLE001 — one record never stops the run
                logger.exception(
                    "sweep.checkout: unexpected error",
                    extra={"checkoutId": checkout_id, "correlationId": correlation_id},
                )
                outcome = self._record_checkout_failure(
                    checkout_id, f"UNEXPECTED:{type(exc).__name__}", correlation_id
                )
            if outcome:
                counts[outcome] += 1
        return counts

    def _sweep_checkout(self, checkout_id: str, correlation_id: str, now: datetime) -> str | None:
        if not self._repo.claim_checkout(checkout_id, self._owner, self._lease_seconds):
            return None  # another worker has it
        checkout = self._repo.get_checkout_by_id(checkout_id)
        if checkout is None or checkout.status != CheckoutStatus.IN_PROGRESS:
            self._repo.release_checkout(checkout_id, self._owner)
            return None
        checkout.claim_owner = self._owner
        try:
            pd1 = self._checkout_service.cancel_if_cutoff_passed(checkout, now)
            if pd1 == "cancelled":
                return self._checkout_outcome(checkout_id, "cancelled", correlation_id)
            if pd1 == "charged":
                self._metrics.emit(f"{_CHECKOUT_FLOW}.charged_after_cutoff")
            self._metrics.emit(f"{_CHECKOUT_FLOW}.resumed")
            self._checkout_service.resume(checkout, correlation_id)
        except (InsufficientBalanceError, WalletNotActiveError):
            return self._checkout_outcome(checkout_id, "payment_failed", correlation_id)
        except LeaseLostError:
            logger.warning(
                "sweep.checkout: lease lost mid-run, stopping",
                extra={"checkoutId": checkout_id, "correlationId": correlation_id},
            )
            return "lease_lost"
        except (CheckoutIncompleteError, WalletUnavailableError) as exc:
            cause = exc.__cause__ if isinstance(exc, CheckoutIncompleteError) else exc
            code = getattr(cause, "error_code", None) or exc.error_code
            return self._record_checkout_failure(checkout_id, code, correlation_id)
        return self._checkout_outcome(checkout_id, "completed", correlation_id)

    def _record_checkout_failure(
        self, checkout_id: str, error_code: str, correlation_id: str
    ) -> str:
        self._metrics.emit(f"{_CHECKOUT_FLOW}.failed_attempt", error=error_code)
        try:
            attempts = self._repo.record_checkout_sweep_failure(
                checkout_id, self._owner, error_code
            )
            if attempts < self._max_attempts:
                self._repo.release_checkout(checkout_id, self._owner)
                self._log_checkout(checkout_id, attempts, "failed_attempt", correlation_id)
                return "failed_attempt"
            return self._exhausted(checkout_id, correlation_id)
        except OrderError:
            # DB trouble too — the lease simply expires and a later run retries.
            logger.exception(
                "sweep.checkout: could not record the failed attempt",
                extra={"checkoutId": checkout_id, "correlationId": correlation_id},
            )
            return "failed_attempt"

    def _exhausted(self, checkout_id: str, correlation_id: str) -> str:
        """MA-144 FR-4 — the budget is spent; what's safe depends on how far
        the checkout got."""
        checkout = self._repo.get_checkout_by_id(checkout_id)
        checkout.claim_owner = self._owner
        if checkout.step == CheckoutStep.PAID:
            # PD-2: paid, subscriptions keep failing — complete with those
            # lines left in the cart rather than holding the order hostage.
            try:
                self._checkout_service.finish_partial(checkout, correlation_id)
            except CheckoutIncompleteError:
                return self._escalate_checkout(checkout_id, "CART_CLEAR_FAILED", None,
                                               correlation_id)
            self._metrics.emit(f"{_CHECKOUT_FLOW}.completed_partial")
            self._metrics.emit(f"{_CHECKOUT_FLOW}.escalated", reason="SUBSCRIPTIONS_ABANDONED")
            self._log_checkout(checkout_id, None, "completed_partial", correlation_id)
            return "completed_partial"
        if checkout.step == CheckoutStep.STARTED:
            # Couldn't find out whether the customer was charged.
            return self._escalate_checkout(
                checkout_id, "CHARGE_UNKNOWN", checkout.order_id, correlation_id
            )
        return self._escalate_checkout(checkout_id, "CART_CLEAR_FAILED", None, correlation_id)

    def _escalate_checkout(
        self, checkout_id: str, reason: str, order_id: str | None, correlation_id: str
    ) -> str:
        self._repo.escalate_checkout(checkout_id, self._owner, reason, order_id)
        self._metrics.emit(f"{_CHECKOUT_FLOW}.escalated", reason=reason)
        self._log_checkout(checkout_id, None, "escalated", correlation_id, reason)
        return "escalated"

    def _checkout_outcome(self, checkout_id: str, outcome: str, correlation_id: str) -> str:
        self._metrics.emit(f"{_CHECKOUT_FLOW}.{outcome}")
        self._log_checkout(checkout_id, None, outcome, correlation_id)
        return outcome

    def _log_checkout(
        self,
        checkout_id: str,
        attempt: int | None,
        outcome: str,
        correlation_id: str,
        reason: str | None = None,
    ) -> None:
        logger.info(
            "sweep.checkout",
            extra={
                "checkoutId": checkout_id,
                "attempt": attempt,
                "outcome": outcome,
                "reason": reason,
                "correlationId": correlation_id,
                "claimOwner": self._owner,
            },
        )

    def _log(
        self,
        order_id: str,
        attempt: int | None,
        outcome: str,
        correlation_id: str,
        reason: str | None = None,
    ) -> None:
        logger.info(
            "sweep.subscription_order",
            extra={
                "orderId": order_id,
                "attempt": attempt,
                "outcome": outcome,
                "reason": reason,
                "correlationId": correlation_id,
                "claimOwner": self._owner,
            },
        )
