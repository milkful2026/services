"""MA-136 — cart checkout: one idempotent, resumable call that turns the
customer's current cart into (a) at most one paid one-time order and (b)
one subscription per subscription line, then clears those lines.

Flow (each step idempotent, so a retry with the same Idempotency-Key
resumes where the last attempt stopped):

    replay?  (user, key) already has a checkout -> return / resume it
    validate (FR-3, nothing persisted): live checkout, cart + version,
             line shape, address, one-time quote, price match, balance
    start    (FR-4, one txn): checkout IN_PROGRESS + CREATED order + items
    charge   (FR-5): wallet debit — PAYMENT_FAILED stops everything
    subs     (FR-6): create each subscription line; a 4xx fails that
             line only, a transport failure pauses the checkout
    clear    (FR-7): remove the checked-out lines from the cart
    done     -> COMPLETED, result stored for replay (FR-9)

Double-charge protection is layered: the client's persisted key, the
`checkouts` (user, key) unique, `orders.checkout_id` unique, and Wallet's
own `order:{orderId}` ledger ref. No external call ever runs inside a DB
transaction.
"""

import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from adapters.order_repository import new_order_id
from domain.checkout_models import (
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CheckoutStep,
    SubscriptionLineResult,
)
from domain.exceptions import (
    AddressLookupUnavailableError,
    CartChangedError,
    CartEmptyError,
    CartUnavailableError,
    CartVersionConflictError,
    CheckoutIncompleteError,
    CheckoutInProgressError,
    DeliveryAddressUnknownError,
    DependencyUnavailableError,
    InsufficientBalanceError,
    LineInvalidError,
    OrderError,
    PriceChangedError,
    PricingUnavailableError,
    ProductPricingUnknownError,
    StoredCheckoutFailureError,
    SubscriptionRejectedError,
    SubscriptionUnavailableError,
    WalletBalanceUnavailableError,
    WalletNotActiveError,
    WalletUnavailableError,
)
from domain.models import Order, OrderItem, OrderSource, OrderStatus

logger = logging.getLogger(__name__)

# Fixed offset, same as Subscription Service's own IST (India has no DST),
# so no dependency on the host's tz database.
IST = timezone(timedelta(hours=5, minutes=30))

# Cart frequency -> Subscription Service schedule type. Cart's Frequency
# stays deliberately narrow (MA-131 §6); WEEKLY/CUSTOM_DAYS are only
# created from the My Subscriptions screen, never through the cart.
_SCHEDULE_FOR_FREQUENCY = {"DAILY": "DAILY", "ALTERNATE_DAYS": "ALTERNATE_DAYS"}

# An IN_PROGRESS checkout untouched this long has no request still driving
# it (every dependency call times out in seconds), so a new-key request
# may resume it instead of being blocked by it forever.
_ABANDONED_AFTER = timedelta(minutes=2)


def new_checkout_id() -> str:
    return f"chk_{uuid.uuid4().hex}"


def to_paise(rupees: float) -> int:
    # Same Decimal rounding as OrderService.materialize — float
    # representation error must never shift a charge by a paisa.
    return int((Decimal(str(rupees)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


class CheckoutService:
    def __init__(
        self,
        repository,
        cart_client,
        user_client,
        pricing_client,
        wallet_client,
        subscription_client,
        *,
        cutoff_hour_ist: int,
        subscription_min_balance_paise: int,
    ) -> None:
        self._repo = repository
        self._cart = cart_client
        self._user = user_client
        self._pricing = pricing_client
        self._wallet = wallet_client
        self._subscriptions = subscription_client
        self._cutoff_hour_ist = cutoff_hour_ist
        self._subscription_min_balance_paise = subscription_min_balance_paise

    # --- FR-1/FR-2 entry point ---

    def checkout(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        cart_version: int,
        expected_pay_now_paise: int | None,
        correlation_id: str,
        now: datetime | None = None,
    ) -> dict:
        existing = self._repo.get_checkout(user_id, idempotency_key)
        if existing is not None:
            logger.info(
                "checkout.replay",
                extra={"checkoutId": existing.id, "status": existing.status.value},
            )
            return self._replay_or_resume(existing, correlation_id)

        live = self._repo.get_live_checkout(user_id)
        if live is not None:
            self._settle_live_checkout(live, correlation_id)

        now = now or datetime.now(IST)
        checkout, order, balance_paise = self._validate_and_build(
            user_id=user_id,
            idempotency_key=idempotency_key,
            cart_version=cart_version,
            expected_pay_now_paise=expected_pay_now_paise,
            now=now,
        )
        started = self._repo.start_checkout(checkout, order)
        if started.id != checkout.id:
            # Lost a race to an identical request with the same key — that
            # one owns the checkout; resume it rather than starting another.
            return self._replay_or_resume(started, correlation_id)
        logger.info(
            "checkout.started",
            extra={
                "checkoutId": checkout.id,
                "userId": user_id,
                "orderId": checkout.order_id,
                "correlationId": correlation_id,
            },
        )
        return self._run(started, correlation_id, known_balance_paise=balance_paise)

    def _replay_or_resume(self, checkout: Checkout, correlation_id: str) -> dict:
        if checkout.status == CheckoutStatus.COMPLETED:
            return checkout.result
        if checkout.status == CheckoutStatus.PAYMENT_FAILED:
            error = (checkout.result or {}).get("error", {})
            raise StoredCheckoutFailureError(
                error.get("errorCode", "INSUFFICIENT_BALANCE"),
                int(error.get("httpStatus", 402)),
                error.get("message", "Payment failed"),
                error.get("details"),
            )
        return self._run(checkout, correlation_id, known_balance_paise=None)

    def _settle_live_checkout(self, live: Checkout, correlation_id: str) -> None:
        """A different key found this user's IN_PROGRESS checkout. If
        another request may still be driving it, reject (naming it so the
        app can resume it). If it was abandoned — the app lost its key
        after a CHECKOUT_INCOMPLETE — finish it here, so it can never lock
        the user out; this request then validates against whatever cart
        that leaves (usually CART_CHANGED, so the app re-reviews)."""
        updated_at = live.updated_at
        if updated_at is not None and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)  # SQLite drops the offset
        if updated_at is None or datetime.now(UTC) - updated_at < _ABANDONED_AFTER:
            raise CheckoutInProgressError(
                "Another checkout is already in progress for this account",
                {"checkoutId": live.id},
            )
        logger.info(
            "checkout.resume_abandoned",
            extra={"checkoutId": live.id, "correlationId": correlation_id},
        )
        try:
            self._run(live, correlation_id, known_balance_paise=None)
        except (InsufficientBalanceError, WalletNotActiveError):
            # The abandoned checkout is now PAYMENT_FAILED — terminal, so
            # it no longer blocks. This request runs its own balance check.
            pass

    # --- FR-3/FR-4: validation (no side effects) + the records to start ---

    def _validate_and_build(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        cart_version: int,
        expected_pay_now_paise: int | None,
        now: datetime,
    ) -> tuple[Checkout, Order | None, int]:
        try:
            cart = self._cart.get_cart(user_id)
        except CartUnavailableError as exc:
            raise DependencyUnavailableError("Cart is unavailable", {"cause": "CART"}) from exc
        if not cart.items:
            raise CartEmptyError("The cart is empty")
        if cart.cart_version != cart_version:
            raise CartChangedError(
                "The cart changed since it was reviewed",
                {"cartVersion": cart.cart_version},
            )

        today = now.astimezone(IST).date()
        lines = [_line_from_cart(item) for item in cart.items]
        invalid = [
            {"lineId": line.line_id, "reason": reason}
            for line in lines
            if (reason := _line_problem(line, today)) is not None
        ]
        if invalid:
            raise LineInvalidError("Some cart lines can't be checked out", {"lines": invalid})

        try:
            delivery_state = self._user.get_delivery_address_state(user_id)
        except AddressLookupUnavailableError as exc:
            raise DependencyUnavailableError(
                "Address lookup is unavailable", {"cause": "USER"}
            ) from exc
        if delivery_state is None:
            raise DeliveryAddressUnknownError("No default delivery address on file")

        one_time = [line for line in lines if not line.is_subscription]
        has_subscriptions = any(line.is_subscription for line in lines)
        pay_now_paise = 0
        if one_time:
            try:
                quote = self._pricing.quote_items(
                    [(line.product_id, line.quantity) for line in one_time], delivery_state
                )
            except ProductPricingUnknownError as exc:
                unknown = exc.details.get("productId")
                bad = [line for line in one_time if unknown is None or line.product_id == unknown]
                raise LineInvalidError(
                    "Some cart lines can't be checked out",
                    {
                        "lines": [
                            {"lineId": line.line_id, "reason": "PRODUCT_UNAVAILABLE"}
                            for line in bad
                        ]
                    },
                ) from exc
            except PricingUnavailableError as exc:
                raise DependencyUnavailableError(
                    "Pricing is unavailable", {"cause": "PRICING"}
                ) from exc
            pay_now_paise = to_paise(quote.net_payable)

        if expected_pay_now_paise is not None and expected_pay_now_paise != pay_now_paise:
            raise PriceChangedError(
                "Prices changed since the cart was reviewed", {"payNowPaise": pay_now_paise}
            )

        try:
            balance_paise = self._wallet.get_balance(user_id)
        except WalletBalanceUnavailableError as exc:
            raise DependencyUnavailableError(
                "Wallet is unavailable", {"cause": "WALLET"}
            ) from exc
        required_paise = pay_now_paise + (
            self._subscription_min_balance_paise if has_subscriptions else 0
        )
        if balance_paise < required_paise:
            raise InsufficientBalanceError(
                "Not enough wallet balance",
                {
                    "balancePaise": balance_paise,
                    "requiredPaise": required_paise,
                    "shortfallPaise": required_paise - balance_paise,
                },
            )

        delivery_date = self._delivery_date(now)
        checkout_id = new_checkout_id()
        order = None
        # Every one-time line gets an order, even one that prices to ₹0
        # (a 100% offer): the order is what records the delivery, and the
        # clear step removes these lines from the cart regardless.
        if one_time:
            order = Order(
                id=new_order_id(),
                user_id=user_id,
                subscription_id=None,
                product_id=None,
                quantity=None,
                amount_paise=pay_now_paise,
                delivery_date=delivery_date,
                status=OrderStatus.CREATED,
                source=OrderSource.CHECKOUT,
                checkout_id=checkout_id,
                items=[OrderItem(line.product_id, line.quantity) for line in one_time],
            )
        checkout = Checkout(
            id=checkout_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            cart_version=cart.cart_version,
            status=CheckoutStatus.IN_PROGRESS,
            step=CheckoutStep.STARTED,
            lines=lines,
            pay_now_paise=pay_now_paise,
            delivery_date=delivery_date,
            order_id=order.id if order else None,
        )
        return checkout, order, balance_paise

    def _delivery_date(self, now: datetime) -> date:
        """FR-8 — tomorrow if confirmed before the IST cut-off, else the
        day after."""
        local = now.astimezone(IST)
        days = 1 if local.hour < self._cutoff_hour_ist else 2
        return local.date() + timedelta(days=days)

    # --- FR-5..FR-7: the resumable steps ---

    def _run(self, checkout: Checkout, correlation_id: str, known_balance_paise: int | None):
        balance_after = known_balance_paise
        if checkout.step == CheckoutStep.STARTED:
            balance_after = self._charge(checkout, correlation_id, balance_after)
            self._repo.update_checkout(checkout.id, step=CheckoutStep.PAID)
            checkout.step = CheckoutStep.PAID
        if checkout.step == CheckoutStep.PAID:
            self._start_subscriptions(checkout, correlation_id)
            self._repo.update_checkout(checkout.id, step=CheckoutStep.SUBSCRIPTIONS_DONE)
            checkout.step = CheckoutStep.SUBSCRIPTIONS_DONE
        self._clear_cart(checkout)

        result = self._result(checkout, balance_after)
        self._repo.update_checkout(
            checkout.id, status=CheckoutStatus.COMPLETED, result=result
        )
        logger.info(
            "checkout.completed",
            extra={"checkoutId": checkout.id, "orderId": checkout.order_id},
        )
        return result

    def _charge(
        self, checkout: Checkout, correlation_id: str, balance_after: int | None
    ) -> int | None:
        if checkout.order_id is None:
            return balance_after  # subscription-only cart — nothing charged now

        order = self._repo.get(checkout.order_id)
        if order.status == OrderStatus.CONFIRMED:
            return balance_after  # a previous attempt charged and confirmed it
        if order.status == OrderStatus.PAYMENT_FAILED:
            # Crashed after recording the failure but before the checkout
            # was marked — finish failing it the same way.
            self._fail_checkout(checkout, order.failure_reason or "INSUFFICIENT_BALANCE", None)
        if order.amount_paise == 0:
            # Nothing to debit — confirm directly rather than asking
            # Wallet for a zero-amount debit.
            now = datetime.now(UTC)
            payload = self._confirmed_payload(order, checkout, now, correlation_id)
            self._repo.mark_confirmed(order.id, now, "OrderConfirmed", payload)
            logger.info("checkout.paid", extra={"checkoutId": checkout.id, "orderId": order.id})
            return balance_after

        try:
            debit = self._wallet.debit(
                checkout.user_id, order.id, order.amount_paise, correlation_id
            )
        except WalletUnavailableError as exc:
            logger.warning("checkout.incomplete", extra={"checkoutId": checkout.id, "at": "charge"})
            raise CheckoutIncompleteError(
                "Couldn't finish placing the order — retry to continue",
                {"checkoutId": checkout.id},
            ) from exc

        if debit.status == "DEBITED":
            now = datetime.now(UTC)
            payload = self._confirmed_payload(order, checkout, now, correlation_id)
            self._repo.mark_confirmed(order.id, now, "OrderConfirmed", payload)
            logger.info("checkout.paid", extra={"checkoutId": checkout.id, "orderId": order.id})
            return debit.balance_after_paise

        self._repo.mark_payment_failed(
            order.id,
            debit.status,
            "OrderPaymentFailed",
            {
                "eventId": str(uuid.uuid4()),
                "occurredAt": datetime.now(UTC).isoformat(),
                "correlationId": correlation_id or str(uuid.uuid4()),
                "orderId": order.id,
                "userId": order.user_id,
                "source": OrderSource.CHECKOUT.value,
                "subscriptionId": None,
                "checkoutId": checkout.id,
                "amountPaise": order.amount_paise,
                "reason": debit.status,
            },
        )
        self._fail_checkout(checkout, debit.status, debit.balance_after_paise)

    def _fail_checkout(self, checkout: Checkout, reason: str, balance_paise: int | None):
        """FR-5 — a declined charge ends the checkout: no subscription is
        created and the cart is left untouched. Stored so a same-key
        replay returns the identical error."""
        if reason == "WALLET_NOT_ACTIVE":
            exc: OrderError = WalletNotActiveError(
                "The wallet isn't active yet", {"checkoutId": checkout.id}
            )
        else:
            required = checkout.pay_now_paise + (
                self._subscription_min_balance_paise if checkout.subscription_lines else 0
            )
            details = {"checkoutId": checkout.id, "requiredPaise": required}
            if balance_paise is not None:
                details["balancePaise"] = balance_paise
                details["shortfallPaise"] = max(required - balance_paise, 0)
            exc = InsufficientBalanceError("Not enough wallet balance", details)
        self._repo.update_checkout(
            checkout.id,
            status=CheckoutStatus.PAYMENT_FAILED,
            result={
                "error": {
                    "errorCode": exc.error_code,
                    "httpStatus": exc.http_status,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
        logger.info(
            "checkout.payment_failed", extra={"checkoutId": checkout.id, "reason": reason}
        )
        raise exc

    def _confirmed_payload(
        self, order: Order, checkout: Checkout, now: datetime, correlation_id: str
    ) -> dict:
        return {
            "eventId": str(uuid.uuid4()),
            "occurredAt": now.isoformat(),
            "correlationId": correlation_id or str(uuid.uuid4()),
            "orderId": order.id,
            "userId": order.user_id,
            "source": OrderSource.CHECKOUT.value,
            "subscriptionId": None,
            "checkoutId": checkout.id,
            "productId": None,
            "quantity": None,
            "items": [
                {"productId": item.product_id, "quantity": item.quantity}
                for item in order.item_list()
            ],
            "amountPaise": order.amount_paise,
            "deliveryDate": order.delivery_date.isoformat(),
        }

    def _start_subscriptions(self, checkout: Checkout, correlation_id: str) -> None:
        done = {r.line_id for r in checkout.subscription_results}
        for line in checkout.subscription_lines:
            if line.line_id in done:
                continue
            try:
                created = self._subscriptions.create(
                    user_id=checkout.user_id,
                    product_id=line.product_id,
                    quantity=line.quantity,
                    schedule_type=_SCHEDULE_FOR_FREQUENCY[line.frequency],
                    start_date=line.start_date,
                    slot_id=line.slot_id,
                    # Derived per line, so a resumed checkout gets the same
                    # subscription back instead of creating a second one.
                    idempotency_key=f"checkout:{checkout.id}:{line.line_id}",
                    correlation_id=correlation_id,
                )
                result = SubscriptionLineResult(
                    line_id=line.line_id,
                    product_id=line.product_id,
                    status="CREATED",
                    subscription_id=created["subscriptionId"],
                    next_delivery_date=created.get("nextDeliveryDate"),
                )
                logger.info(
                    "checkout.subscription_created",
                    extra={"checkoutId": checkout.id, "lineId": line.line_id},
                )
            except SubscriptionRejectedError as exc:
                # FR-6 (Product, 2026-09-25): one line failing doesn't block
                # the rest; the line stays in the cart.
                result = SubscriptionLineResult(
                    line_id=line.line_id,
                    product_id=line.product_id,
                    status="FAILED",
                    reason=exc.reason,
                )
                logger.info(
                    "checkout.subscription_failed",
                    extra={
                        "checkoutId": checkout.id,
                        "lineId": line.line_id,
                        "reason": exc.reason,
                    },
                )
            except SubscriptionUnavailableError as exc:
                logger.warning(
                    "checkout.incomplete", extra={"checkoutId": checkout.id, "at": "subscriptions"}
                )
                raise CheckoutIncompleteError(
                    "Couldn't finish placing the order — retry to continue",
                    {"checkoutId": checkout.id},
                ) from exc
            checkout.subscription_results.append(result)
            self._repo.update_checkout(
                checkout.id, subscription_results=checkout.subscription_results
            )

    def _clear_cart(self, checkout: Checkout) -> None:
        created = {r.line_id for r in checkout.subscription_results if r.status == "CREATED"}
        # One-time lines were paid for if we got here (a failed charge
        # never reaches this step).
        item_ids = [line.line_id for line in checkout.one_time_lines] + [
            line.line_id for line in checkout.subscription_lines if line.line_id in created
        ]
        if not item_ids:
            return
        try:
            try:
                self._cart.remove_items(
                    checkout.user_id, item_ids, checkout.cart_version, checkout.id
                )
            except CartVersionConflictError:
                # The customer edited the cart from another device mid-
                # checkout: re-read once and remove only the lines still
                # exactly as checked out. A line edited since (say, quantity
                # 1 -> 5) wasn't what we charged for or subscribed to, so it
                # stays in the cart for the customer to check out again.
                current = self._cart.get_cart(checkout.user_id)
                snapshot = {line.line_id: line for line in checkout.lines}
                unchanged_ids = {
                    item["id"]
                    for item in current.items
                    if _line_from_cart(item) == snapshot.get(item["id"])
                }
                remaining = [item_id for item_id in item_ids if item_id in unchanged_ids]
                edited = [
                    item["id"]
                    for item in current.items
                    if item["id"] in item_ids and item["id"] not in unchanged_ids
                ]
                if edited:
                    logger.info(
                        "checkout.clear_skipped_edited_lines",
                        extra={"checkoutId": checkout.id, "lineIds": edited},
                    )
                if remaining:
                    self._cart.remove_items(
                        checkout.user_id, remaining, current.cart_version, checkout.id
                    )
        except (CartUnavailableError, CartVersionConflictError) as exc:
            logger.warning("checkout.incomplete", extra={"checkoutId": checkout.id, "at": "clear"})
            raise CheckoutIncompleteError(
                "Couldn't finish placing the order — retry to continue",
                {"checkoutId": checkout.id},
            ) from exc
        logger.info(
            "checkout.cart_cleared",
            extra={"checkoutId": checkout.id, "removedCount": len(item_ids)},
        )

    # --- FR-9 ---

    def _result(self, checkout: Checkout, balance_after_paise: int | None) -> dict:
        order_body = None
        if checkout.order_id is not None:
            order = self._repo.get(checkout.order_id)
            order_body = {
                "orderId": order.id,
                "status": order.status.value,
                "amountPaise": order.amount_paise,
                "deliveryDate": order.delivery_date.isoformat(),
                "items": [
                    {"productId": item.product_id, "quantity": item.quantity}
                    for item in order.item_list()
                ],
            }
        if balance_after_paise is None:
            try:
                balance_after_paise = self._wallet.get_balance(checkout.user_id)
            except WalletBalanceUnavailableError:
                balance_after_paise = None  # cosmetic only — never fail a placed order on it
        return {
            "checkoutId": checkout.id,
            "status": CheckoutStatus.COMPLETED.value,
            "order": order_body,
            "subscriptions": [r.to_dict() for r in checkout.subscription_results],
            "walletBalanceAfterPaise": balance_after_paise,
        }


def _line_from_cart(item: dict) -> CheckoutLine:
    start = item.get("startDate")
    return CheckoutLine(
        line_id=item["id"],
        product_id=item["productId"],
        quantity=int(item["quantity"]),
        frequency=item["frequency"],
        # The app has historically sent a full ISO timestamp
        # ("2026-09-27T00:00:00.000") and Cart stores it verbatim — the
        # date part is what the customer picked.
        start_date=_parse_date(start),
        slot_id=item.get("slotId"),
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _line_problem(line: CheckoutLine, today: date) -> str | None:
    if not line.is_subscription:
        return None
    if line.frequency not in _SCHEDULE_FOR_FREQUENCY:
        return "UNSUPPORTED_FREQUENCY"
    if not line.slot_id:
        return "SLOT_MISSING"
    if line.start_date is None or line.start_date < today:
        return "START_DATE_PAST"
    return None
