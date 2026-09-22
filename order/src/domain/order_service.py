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
from adapters.order_repository import decode_cursor, new_order_id
from domain.exceptions import InvalidCursorError, OrderNotFoundError, ProductPricingUnknownError
from domain.models import Order, OrdersPage, OrderStatus

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
    ) -> None:
        self._repo = repository
        self._user_client = user_client
        self._pricing_client = pricing_client
        self._wallet_client = wallet_client

    def materialize(
        self,
        *,
        subscription_id: str,
        user_id: str,
        product_id: str,
        quantity: int,
        delivery_date,
        correlation_id: str | None,
    ) -> None:
        existing = self._repo.get_by_subscription_and_date(subscription_id, delivery_date)
        if existing is not None:
            if existing.status == OrderStatus.CREATED:
                # A prior attempt crashed between insert and the debit
                # call — resume there rather than re-inserting or
                # silently dropping the redelivery.
                self._debit_and_finalize(existing, correlation_id)
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
        self._debit_and_finalize(order, correlation_id)

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
            "subscriptionId": subscription_id,
            "amountPaise": 0,
            "reason": reason,
        }
        order = self._repo.insert_payment_failed(order, "OrderPaymentFailed", payload)
        logger.info("order.payment_failed", extra={"orderId": order.id, "reason": reason})

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
            "subscriptionId": order.subscription_id,
            "amountPaise": order.amount_paise,
            "reason": reason,
        }
        self._repo.mark_payment_failed(order.id, reason, "OrderPaymentFailed", payload)
        logger.info("order.payment_failed", extra={"orderId": order.id, "reason": reason})

    # --- FR-3: read APIs ---

    def get(self, order_id: str, user_id: str) -> dict:
        order = self._repo.get(order_id)
        if order is None or order.user_id != user_id:
            # 404, not 403 — don't leak existence to a non-owner.
            raise OrderNotFoundError(f"No order {order_id!r}")
        return _serialize(order)

    def list_for_user(
        self, user_id: str, subscription_id: str | None, limit: int | None, cursor: str | None
    ) -> dict:
        page_size = _DEFAULT_PAGE if not limit else max(1, min(limit, _MAX_PAGE))
        before_seq = None
        if cursor:
            try:
                before_seq = decode_cursor(cursor)
            except Exception as exc:  # noqa: BLE001 — any decode failure is a bad cursor
                raise InvalidCursorError("Malformed pagination cursor") from exc

        page: OrdersPage = self._repo.list_for_user(user_id, subscription_id, page_size, before_seq)
        return {
            "items": [_serialize(o) for o in page.items],
            "nextCursor": page.next_cursor,
        }


def _serialize(order: Order) -> dict:
    return {
        "orderId": order.id,
        "subscriptionId": order.subscription_id,
        "productId": order.product_id,
        "quantity": order.quantity,
        "amountPaise": order.amount_paise,
        "deliveryDate": order.delivery_date.isoformat(),
        "status": order.status.value,
        "failureReason": order.failure_reason,
        "createdAt": order.created_at.isoformat() if order.created_at else None,
        "confirmedAt": order.confirmed_at.isoformat() if order.confirmed_at else None,
    }
