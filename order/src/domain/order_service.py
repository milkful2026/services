"""Order domain service — the only place business rules live.

Covers MA-132: materializing a `SubscriptionOrderDue` into a real, paid
Order (User address lookup -> Pricing quote -> Wallet debit), and the
FR-3 read APIs.

Reconciles an internal contradiction between MA-132.md's own §8
(Integration Considerations, which lumps "Unavailable / no profile
found" into one "fails closed, message not acked" row) and §9 (Edge
Cases, which gives "no profile / no default address" its own
`PAYMENT_FAILED`/`DELIVERY_ADDRESS_UNKNOWN` outcome — implying a real,
acked order record, not silence). This implementation follows §9's more
specific, more actionable framing: a *transient* failure (the address-
state call itself timing out or erroring after retries) fails closed and
leaves the message unacked for retry; a *definite* fact reported by a
successful call (no profile / no default address; Catalog has no such
product) creates a terminal `PAYMENT_FAILED` order and acks — retrying
either fact changes nothing. Same reasoning §9 already applies to
`INSUFFICIENT_BALANCE`/`WALLET_NOT_ACTIVE` vs. a Wallet transport
failure.

A `PAYMENT_FAILED` order created before a price was ever obtained (the
address-unknown and product-unavailable cases, both necessarily *before*
`amount_paise` exists) is recorded with `amount_paise = 0` — the
migration's `CHECK` is `>= 0`, not `> 0`, specifically to allow this;
every `CONFIRMED` order still has a real, positive amount. Unlike the
priced/debit path (insert `CREATED`, then a separate `mark_confirmed`/
`mark_payment_failed` transition once a price and a debit outcome
exist), these two pre-pricing reasons insert the order *already*
`PAYMENT_FAILED`, atomically with their outbox row — there's no useful
"resume" action for a redelivery to take mid-way through a case that
was never going to reach the debit step, and `materialize`'s own
crash-resume path always resumes a `CREATED` order at the debit step,
which would otherwise attempt to debit the `amount_paise = 0`
placeholder.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from adapters.interfaces import (
    PricingClientPort,
    UserClientPort,
    WalletClientPort,
)
from adapters.logging_metrics import LoggingMetricsRecorder
from adapters.order_repository import decode_cursor, new_order_id
from domain.cutoff import delivery_cutoff_moment, delivery_cutoff_passed
from domain.exceptions import (
    CutoffPassedError,
    DebitNotFoundError,
    DebitVoidedError,
    InvalidCursorError,
    OrderBusyError,
    OrderError,
    OrderNotCancellableError,
    OrderNotFoundError,
    OrderUserMismatchError,
    ProductPricingUnknownError,
    RefundExceedsDebitError,
    ValidationError,
    WalletUnavailableError,
)
from domain.models import CancelReason, Order, OrdersPage, OrderStatus, RefundState

logger = logging.getLogger(__name__)

_MAX_PAGE = 100
_DEFAULT_PAGE = 20


class OrderService:
    def __init__(
        self,
        repository,
        user_client: UserClientPort,
        pricing_client: PricingClientPort,
        wallet_client: WalletClientPort,
        *,
        lease_seconds: float = 120,
        cutoff_hour_ist: int = 20,
        metrics=None,
    ) -> None:
        self._repo = repository
        self._user_client = user_client
        self._pricing_client = pricing_client
        self._wallet_client = wallet_client
        self._lease_seconds = lease_seconds
        # MA-154: the cancel deadline is the checkout cut-off (Settings).
        self._cutoff_hour_ist = cutoff_hour_ist
        self._metrics = metrics or LoggingMetricsRecorder()

    def materialize(
        self,
        *,
        subscription_id: str,
        user_id: str,
        product_id: str,
        quantity: int,
        delivery_date,
        correlation_id: str | None,
        claim_owner: str | None = None,
    ) -> None:
        existing = self._repo.get_by_subscription_and_date(subscription_id, delivery_date)
        if existing is not None:
            if existing.status == OrderStatus.CREATED:
                # A prior attempt crashed between insert and the debit
                # call — resume there rather than re-inserting or
                # silently dropping the redelivery.
                self._resume_claimed(existing, correlation_id, claim_owner)
                return
            logger.info(
                "order.materialize: already resolved, no-op",
                extra={"orderId": existing.id, "status": existing.status.value},
            )
            return

        # Raises AddressLookupUnavailableError after retries — propagates
        # uncaught; the consumer leaves the message unacked for redelivery.
        delivery_state = self._user_client.get_delivery_address_state(user_id)

        if delivery_state is None:
            self._insert_payment_failed_before_pricing(
                subscription_id=subscription_id,
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
                delivery_date=delivery_date,
                reason="DELIVERY_ADDRESS_UNKNOWN",
                correlation_id=correlation_id,
            )
            return

        try:
            quote = self._pricing_client.quote(product_id, quantity, delivery_state)
        except ProductPricingUnknownError:
            self._insert_payment_failed_before_pricing(
                subscription_id=subscription_id,
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
                delivery_date=delivery_date,
                reason="PRODUCT_UNAVAILABLE",
                correlation_id=correlation_id,
            )
            return
        # PricingUnavailableError propagates uncaught — message unacked.

        # Pricing/Catalog return rupees; this service (and Wallet's debit
        # call) are paise throughout, matching every other service.
        # Decimal, not round() on the float directly — net_payable is
        # already rounded to 2dp by Pricing Service, but binary-float
        # representation error on that value can shift round(x*100) off
        # by one paise for a value that lands near a rounding boundary.
        amount_paise = int(
            (Decimal(str(quote.net_payable)) * 100).to_integral_value(rounding=ROUND_HALF_UP)
        )
        order = self._repo.insert_created(
            Order(
                id=new_order_id(),
                user_id=user_id,
                subscription_id=subscription_id,
                product_id=product_id,
                quantity=quantity,
                amount_paise=amount_paise,
                delivery_date=delivery_date,
                status=OrderStatus.CREATED,
            )
        )
        try:
            self._debit_and_finalize(order, correlation_id)
        except DebitVoidedError:
            self._debit_refused_voided(order)

    def _insert_payment_failed_before_pricing(
        self,
        *,
        subscription_id: str,
        user_id: str,
        product_id: str,
        quantity: int,
        delivery_date,
        reason: str,
        correlation_id: str | None,
    ) -> None:
        """Shared by the two failure paths that precede ever getting a
        price (no default address on file; Catalog has no such product)
        — both necessarily amount_paise=0. Inserted directly as a
        terminal PAYMENT_FAILED row (see insert_payment_failed's own
        docstring for why this must be one atomic write rather than
        insert-CREATED-then-fail)."""
        order = Order(
            id=new_order_id(),
            user_id=user_id,
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            amount_paise=0,
            delivery_date=delivery_date,
            status=OrderStatus.PAYMENT_FAILED,
            failure_reason=reason,
        )
        payload = {
            "eventId": str(uuid.uuid4()),
            "occurredAt": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id or "",
            "orderId": order.id,
            "userId": user_id,
            "source": "SUBSCRIPTION",
            "subscriptionId": subscription_id,
            "amountPaise": 0,
            "reason": reason,
        }
        order = self._repo.insert_payment_failed(order, "OrderPaymentFailed", payload)
        logger.info("order.payment_failed", extra={"orderId": order.id, "reason": reason})

    def _resume_claimed(
        self, order: Order, correlation_id: str | None, claim_owner: str | None
    ) -> None:
        """MA-143 FR-5 — the SQS resume path takes the order's lease first,
        so it never debits alongside the sweep. Lost -> OrderBusyError (the
        consumer leaves the message unacked; the redelivery then finds the
        order terminal). A terminal transition releases the lease itself.

        `DebitVoidedError`: the sweep closed (or is closing) this order past
        its charge deadline — release, log and ack; the sweep finishes it."""
        if claim_owner is not None and not self._repo.claim_order(
            order.id, claim_owner, self._lease_seconds
        ):
            raise OrderBusyError(
                "Order is being resumed by another worker", {"orderId": order.id}
            )
        try:
            self._debit_and_finalize(order, correlation_id)
        except WalletUnavailableError:
            if claim_owner is not None:
                self._repo.release_order(order.id, claim_owner)
            raise
        except DebitVoidedError:
            if claim_owner is not None:
                self._repo.release_order(order.id, claim_owner)
            self._debit_refused_voided(order)

    @staticmethod
    def _debit_refused_voided(order: Order) -> None:
        logger.warning("order.debit_refused_voided", extra={"orderId": order.id})

    def resume_debit(self, order: Order, correlation_id: str | None) -> None:
        """MA-143 — the sweep's entry point: the same debit step the SQS
        crash-resume path runs. The caller already holds the lease and
        handles WalletUnavailableError / DebitVoidedError itself."""
        self._debit_and_finalize(order, correlation_id)

    def _debit_and_finalize(self, order: Order, correlation_id: str | None) -> None:
        # Deliberately outside any DB transaction — an external HTTP call
        # held inside one is the exact anti-pattern this codebase avoids
        # elsewhere. Raises WalletUnavailableError after retries (or a
        # persistent 503 WALLET_PROVISIONING_PENDING) — propagates
        # uncaught, order stays CREATED, message unacked; a redelivery
        # resumes here (this same method) rather than re-inserting.
        result = self._wallet_client.debit(
            order.user_id, order.id, order.amount_paise, correlation_id or ""
        )
        if result.status == "DEBITED":
            now = datetime.now(UTC)
            payload = {
                "eventId": str(uuid.uuid4()),
                "occurredAt": now.isoformat(),
                "correlationId": correlation_id or "",
                "orderId": order.id,
                "userId": order.user_id,
                "source": "SUBSCRIPTION",
                "subscriptionId": order.subscription_id,
                "productId": order.product_id,
                "quantity": order.quantity,
                "amountPaise": order.amount_paise,
                "deliveryDate": order.delivery_date.isoformat(),
            }
            self._repo.mark_confirmed(order.id, now, "OrderConfirmed", payload)
            logger.info("order.confirmed", extra={"orderId": order.id})
            return
        # INSUFFICIENT_BALANCE or WALLET_NOT_ACTIVE — both normal 200
        # outcomes per MA-130's contract, not exceptions.
        self._fail_payment(order, result.status, correlation_id)

    def _fail_payment(self, order: Order, reason: str, correlation_id: str | None) -> None:
        payload = {
            "eventId": str(uuid.uuid4()),
            "occurredAt": datetime.now(UTC).isoformat(),
            "correlationId": correlation_id or "",
            "orderId": order.id,
            "userId": order.user_id,
            "source": "SUBSCRIPTION",
            "subscriptionId": order.subscription_id,
            "amountPaise": order.amount_paise,
            "reason": reason,
        }
        self._repo.mark_payment_failed(order.id, reason, "OrderPaymentFailed", payload)
        logger.info("order.payment_failed", extra={"orderId": order.id, "reason": reason})

    # --- MA-154: customer cancellation ---

    def cancel(
        self,
        order_id: str,
        user_id: str,
        reason: object,
        now: datetime,
        correlation_id: str,
    ) -> dict:
        """`POST /orders/{id}/cancel`. Step A cancels and enqueues
        OrderCancelled in one transaction; Step B then refunds through Wallet
        (MA-153). Cancelling first means a cancelled order is never
        delivered; a refund that can't complete now stays PENDING for the
        sweep, and the request still succeeds. A repeat cancel is a replay
        (200 with the current state), finishing a PENDING refund first."""
        cancel_reason = _parse_reason(reason)
        order = self._repo.get(order_id)
        if self._check_cancellable(order, order_id, user_id, now):
            return self._replay(order, now, correlation_id)

        refund_state = RefundState.PENDING if order.amount_paise > 0 else RefundState.NOT_REQUIRED
        won = self._repo.cancel_by_customer(
            order.id,
            reason=cancel_reason,
            now=now,
            refund_state=refund_state,
            outbox_payload=_order_cancelled_payload(
                order, cancel_reason, refund_state, now, correlation_id
            ),
        )
        if not won:
            # A concurrent cancel or another transition got there first.
            order = self._repo.get(order_id)
            self._check_cancellable(order, order_id, user_id, now)  # raises unless a replay
            return self._replay(order, now, correlation_id)

        if refund_state == RefundState.PENDING:
            self.finish_refund(order, correlation_id)
        cancelled = self._repo.get(order.id)
        self._log_cancel(cancelled, "cancelled", correlation_id)
        return _serialize(cancelled, now, self._cutoff_hour_ist)

    def _check_cancellable(
        self, order: Order | None, order_id: str, user_id: str, now: datetime
    ) -> bool:
        """FR-2, in order. True for a replay (already cancelled by its
        customer); otherwise raises unless the order may be cancelled now."""
        if order is None or order.user_id != user_id:
            # 404, not 403 — don't leak existence to a non-owner.
            raise OrderNotFoundError(f"No order {order_id!r}")
        if order.is_customer_cancelled:
            return True
        if order.status != OrderStatus.CONFIRMED:
            raise OrderNotCancellableError(
                "Only a confirmed order can be cancelled", {"status": order.status.value}
            )
        if delivery_cutoff_passed(order.delivery_date, now, self._cutoff_hour_ist):
            until = delivery_cutoff_moment(order.delivery_date, self._cutoff_hour_ist)
            raise CutoffPassedError(
                "The cancellation cut-off has passed", {"cancellableUntil": until.isoformat()}
            )
        return False

    def _replay(self, order: Order, now: datetime, correlation_id: str) -> dict:
        if order.refund_state == RefundState.PENDING:
            # A customer retry can finish the refund sooner than the sweep.
            self.finish_refund(order, correlation_id)
            order = self._repo.get(order.id)
        self._log_cancel(order, "replayed", correlation_id)
        return _serialize(order, now, self._cutoff_hour_ist)

    def finish_refund(self, order: Order, correlation_id: str, *, owner: str | None = None) -> str:
        """FR-3 Step B, shared by the cancel request and the sweep (FR-5).
        Never raises: a refund that can't complete stays PENDING. Returns
        `refunded`, `not_required`, `still_pending` or `error`. With `owner`
        (the sweep), the lease is released whatever happens."""
        try:
            self._wallet_client.refund(
                order.user_id, order.id, _REFUND_ID, order.amount_paise, correlation_id
            )
        except DebitNotFoundError:
            # A CONFIRMED order should always have a debit: investigate.
            logger.warning(
                "order.refund: no debit found, nothing to refund",
                extra={"orderId": order.id, "correlationId": correlation_id},
            )
            self._repo.mark_refund_state(order.id, RefundState.NOT_REQUIRED, owner=owner)
            return self._refund_outcome(order.id, "not_required")
        except (RefundExceedsDebitError, OrderUserMismatchError) as exc:
            # A data bug: alarmed, left PENDING for support.
            logger.error(
                "order.refund.data_error",
                extra={
                    "metric": "order.refund.data_error",
                    "orderId": order.id,
                    "errorCode": exc.error_code,
                    "correlationId": correlation_id,
                },
            )
            self._release(order.id, owner)
            return self._refund_outcome(order.id, "error")
        except Exception:  # noqa: BLE001 — Wallet down, timeout, anything: retried by the sweep
            logger.warning(
                "order.refund: still pending",
                exc_info=True,
                extra={"orderId": order.id, "correlationId": correlation_id},
            )
            self._release(order.id, owner)
            return self._refund_outcome(order.id, "still_pending")
        self._repo.mark_refund_state(
            order.id, RefundState.REFUNDED, refunded_at=datetime.now(UTC), owner=owner
        )
        return self._refund_outcome(order.id, "refunded")

    def _release(self, order_id: str, owner: str | None) -> None:
        if owner is None:
            return
        try:
            self._repo.release_order(order_id, owner)
        except OrderError:
            pass  # the lease expires on its own

    def _refund_outcome(self, order_id: str, outcome: str) -> str:
        self._metrics.emit("order.refund.outcome", outcome=outcome, orderId=order_id)
        return outcome

    def _log_cancel(self, order: Order, outcome: str, correlation_id: str) -> None:
        refund_state = order.refund_state.value if order.refund_state else None
        logger.info(
            "order.cancel",
            extra={
                "orderId": order.id,
                "outcome": outcome,
                "refundState": refund_state,
                "correlationId": correlation_id,
            },
        )
        self._metrics.emit("order.cancel.count", outcome=outcome)

    # --- FR-3: read APIs ---

    def get(self, order_id: str, user_id: str, now: datetime | None = None) -> dict:
        order = self._repo.get(order_id)
        if order is None or order.user_id != user_id:
            # 404, not 403 — don't leak existence to a non-owner.
            raise OrderNotFoundError(f"No order {order_id!r}")
        return _serialize(order, now or datetime.now(UTC), self._cutoff_hour_ist)

    def list_for_user(
        self,
        user_id: str,
        subscription_id: str | None,
        limit: int | None,
        cursor: str | None,
        now: datetime | None = None,
    ) -> dict:
        page_size = _DEFAULT_PAGE if not limit else max(1, min(limit, _MAX_PAGE))
        before_seq = None
        if cursor:
            try:
                before_seq = decode_cursor(cursor)
            except Exception as exc:  # noqa: BLE001 — any decode failure is a bad cursor
                raise InvalidCursorError("Malformed pagination cursor") from exc

        page: OrdersPage = self._repo.list_for_user(user_id, subscription_id, page_size, before_seq)
        now = now or datetime.now(UTC)
        return {
            "items": [_serialize(o, now, self._cutoff_hour_ist) for o in page.items],
            "nextCursor": page.next_cursor,
        }


_REFUND_ID = "cancel"  # MA-153: MA-32 refunds each order once, in full.


def _parse_reason(reason: object) -> CancelReason | None:
    if reason is None:
        return None
    if isinstance(reason, str) and reason in CancelReason.__members__:
        return CancelReason(reason)
    raise ValidationError(
        "reason must be one of " + ", ".join(CancelReason), {"field": "reason"}
    )


def _order_cancelled_payload(
    order: Order,
    reason: CancelReason | None,
    refund_state: RefundState,
    now: datetime,
    correlation_id: str,
) -> dict:
    """MA-154 FR-6. `items` lists every line, a subscription order's single
    product included: the schema has no productId, and consumers (e.g.
    Delivery dropping the stop) need to know what was cancelled."""
    return {
        "eventId": str(uuid.uuid4()),
        "occurredAt": now.astimezone(UTC).isoformat(),
        "correlationId": correlation_id,
        "orderId": order.id,
        "userId": order.user_id,
        "source": order.source.value,
        "subscriptionId": order.subscription_id,
        "checkoutId": order.checkout_id,
        "items": [
            {"productId": item.product_id, "quantity": item.quantity}
            for item in order.item_list()
        ],
        "amountPaise": order.amount_paise,
        "deliveryDate": order.delivery_date.isoformat(),
        "cancelledBy": "CUSTOMER",
        "cancelReason": reason.value if reason else None,
        "refundState": refund_state.value,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize(order: Order, now: datetime, cutoff_hour_ist: int) -> dict:
    return {
        "orderId": order.id,
        # MA-136 FR-10 — SUBSCRIPTION | CHECKOUT; `items` lists every line
        # for both kinds (a subscription order is its single product).
        "source": order.source.value,
        "checkoutId": order.checkout_id,
        "items": [
            {"productId": item.product_id, "quantity": item.quantity}
            for item in order.item_list()
        ],
        "subscriptionId": order.subscription_id,
        "productId": order.product_id,
        "quantity": order.quantity,
        "amountPaise": order.amount_paise,
        "deliveryDate": order.delivery_date.isoformat(),
        "status": order.status.value,
        "failureReason": order.failure_reason,
        "createdAt": order.created_at.isoformat() if order.created_at else None,
        "confirmedAt": order.confirmed_at.isoformat() if order.confirmed_at else None,
        # MA-154 FR-7 — additive; `cancellableUntil` only while it's still open.
        "cancellableUntil": _cancellable_until(order, now, cutoff_hour_ist),
        "cancelReason": order.cancel_reason.value if order.cancel_reason else None,
        "cancelledAt": _iso(order.cancelled_at),
        "refundState": order.refund_state.value if order.refund_state else None,
    }


def _cancellable_until(order: Order, now: datetime, cutoff_hour_ist: int) -> str | None:
    if order.status != OrderStatus.CONFIRMED:
        return None
    until = delivery_cutoff_moment(order.delivery_date, cutoff_hour_ist)
    return until.isoformat() if now < until else None
