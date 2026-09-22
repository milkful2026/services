"""Ports the domain depends on. Adapters implement these; the domain
never imports SQLAlchemy, `requests`, or boto3 directly."""

from datetime import datetime
from typing import Protocol

from domain.models import DebitResult, Order, OrdersPage, Quote


class OrderRepositoryPort(Protocol):
    def get_by_subscription_and_date(self, subscription_id: str, delivery_date) -> Order | None:
        """The FR-1 idempotency check — a UNIQUE(subscription_id,
        delivery_date) row already existing means this SubscriptionOrderDue
        was already (at least partially) processed."""
        ...

    def insert_created(self, order: Order) -> None:
        """Inserts the Order row `CREATED` only — no outbox row. Relies on
        the `UNIQUE(subscription_id, delivery_date)` constraint to make a
        concurrent duplicate insert safe (raises, caller treats it as
        already-being-processed rather than a hard failure)."""
        ...

    def mark_confirmed(
        self, order_id: str, confirmed_at: datetime, outbox_event_type: str, outbox_payload: dict
    ) -> Order:
        """One transaction: status -> CONFIRMED, confirmed_at set, and the
        one OrderConfirmed outbox row inserted — but only if the order is
        still CREATED. A concurrent redelivery that already transitioned
        this order is a no-op: no second update, no second outbox row."""
        ...

    def mark_payment_failed(
        self,
        order_id: str,
        failure_reason: str,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> Order:
        """One transaction: status -> PAYMENT_FAILED, failure_reason set,
        and the one OrderPaymentFailed outbox row inserted — but only if
        the order is still CREATED, same concurrent-redelivery guard as
        mark_confirmed."""
        ...

    def insert_payment_failed(
        self, order: Order, outbox_event_type: str, outbox_payload: dict
    ) -> Order:
        """One transaction: inserts the order row already PAYMENT_FAILED
        (amount_paise=0) plus its OrderPaymentFailed outbox row — for a
        pre-pricing failure, where there is no CREATED intermediate state
        to resume from. A concurrent duplicate insert (the same
        UNIQUE(subscription_id, delivery_date) race insert_created
        guards against) returns the winner's row rather than raising."""
        ...

    def get(self, order_id: str) -> Order | None: ...

    def list_for_user(
        self, user_id: str, subscription_id: str | None, limit: int, before_seq: int | None
    ) -> OrdersPage:
        """Keyset-paginated on the internal `seq` column (not the `id`
        business key, which isn't monotonic), newest first — mirrors
        Wallet's own `list_transactions` convention."""
        ...


class UserClientPort(Protocol):
    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        """SigV4-signed. Returns the state string, or None if User Service
        has no profile / no default address for this user (a definite
        fact — not the same as unavailable). Raises
        AddressLookupUnavailableError after retries are exhausted."""
        ...


class PricingClientPort(Protocol):
    def quote(self, product_id: str, quantity: int, delivery_state: str) -> Quote:
        """Raises ProductPricingUnknownError if Catalog has no such
        product (a definite fact), PricingUnavailableError for any other
        failure after retries (transient)."""
        ...


class WalletClientPort(Protocol):
    def debit(
        self, user_id: str, order_id: str, amount_paise: int, correlation_id: str
    ) -> DebitResult:
        """Raises WalletUnavailableError after retries for a transport
        failure or a persistent 503 WALLET_PROVISIONING_PENDING. Returns a
        typed DebitResult (never raises) for DEBITED/INSUFFICIENT_BALANCE/
        WALLET_NOT_ACTIVE — all three are normal 200 responses."""
        ...
