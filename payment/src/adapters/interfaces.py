"""Ports the domain depends on. Adapters implement these; the domain
never imports SQLAlchemy, boto3, or the Razorpay SDK directly."""

from typing import Protocol

from domain.models import Payment


class PaymentRepositoryPort(Protocol):
    def get_by_user_idem(self, user_id: str, idempotency_key: str) -> Payment | None: ...

    def insert_created(
        self,
        *,
        payment_id: str,
        user_id: str,
        purpose: str,
        amount_paise: int,
        currency: str,
        method: str | None,
        idempotency_key: str,
        correlation_id: str,
    ) -> Payment: ...

    def set_order_id(self, payment_id: str, razorpay_order_id: str) -> None: ...

    def lock_by_id(self, payment_id: str) -> Payment | None:
        """SELECT ... FOR UPDATE by primary key."""
        ...

    def lock_by_order_id(self, razorpay_order_id: str) -> Payment | None:
        """SELECT ... FOR UPDATE by razorpay_order_id."""
        ...

    def set_status(
        self,
        payment_id: str,
        *,
        status: str,
        method: str | None = None,
        razorpay_payment_id: str | None = None,
        razorpay_signature: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
        captured_at_now: bool = False,
    ) -> None: ...

    def append_event(self, payment_id: str, source: str, raw_payload: dict) -> None: ...

    def enqueue_outbox(self, payment_id: str, event_type: str, payload: dict) -> None: ...

    def list_stale(
        self, status_in: tuple[str, ...], older_than_seconds: float
    ) -> list[Payment]: ...

    def get(self, payment_id: str) -> Payment | None: ...


class PaymentGatewayPort(Protocol):
    def orders_create(self, *, amount_paise: int, receipt: str, notes: dict) -> str:
        """Returns the Razorpay order id."""
        ...

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool: ...

    def verify_client_signature(
        self, razorpay_order_id: str, razorpay_payment_id: str, signature: str
    ) -> bool: ...

    def fetch_order_payments(self, razorpay_order_id: str) -> list[dict]:
        """Raw Razorpay `payment` entities for the order, newest first."""
        ...


class OutboxPort(Protocol):
    def fetch_unpublished(self, limit: int = 20) -> list[dict]: ...
    def mark_published(self, outbox_id: int) -> None: ...


class WalletLimitsPort(Protocol):
    def get_limits(self) -> tuple[int, int]:
        """Returns (min_paise, max_paise)."""
        ...
