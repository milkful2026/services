"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/requests/boto3 directly."""

from typing import Protocol

from domain.models import Address, Consent, DeliverySlot, RegistrationResult, UserProfile


class UserRepositoryPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_by_cognito_sub(self, cognito_sub: str) -> RegistrationResult | None:
        """Cheap indexed read, no transaction. Returns None if no user
        exists yet for this cognito_sub — callers use this to short-circuit
        a duplicate registration before ever calling register()."""
        ...

    def get_profile_by_sub(self, cognito_sub: str) -> UserProfile | None:
        """Spec MA-107 FR-2 — resolved by the JWT `sub` claim only. None
        if no matching row (caller maps this to 404, not 500)."""
        ...

    def register(
        self,
        cognito_sub: str,
        mobile: str,
        name: str,
        email: str | None,
        addresses: list[Address],
        preferred_slot_id: str | None,
        consents: list[Consent],
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> RegistrationResult:
        """Single DB transaction: insert `users`, insert addresses, insert
        consents, insert one outbox_events row. Idempotent on cognito_sub
        under concurrency too — if a race loses to a concurrent call for
        the same cognito_sub, the existing row is returned with
        is_new_user=False rather than raising. Per spec §9, the outbox
        write happens in the SAME transaction as everything else, so
        "event publish fails" can never mean "user wasn't created" or
        vice versa.
        """
        ...

    def get_delivery_slots(self, zone_id: str) -> list[DeliverySlot]:
        """Reads from this service's own zone_slots reference table —
        see registration_service.py's module docstring for why this
        isn't a live Inventory call."""
        ...

    def get_unpublished_outbox_events(self, limit: int) -> list[dict]:
        """Used only by outbox_publisher_handler, not the request path.
        Returns oldest-unpublished-first."""
        ...

    def mark_outbox_published(self, event_id: str) -> None: ...


class InventoryClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def check_serviceability(self, pincode: str, lat: float, lng: float) -> bool:
        """Raises ExternalServiceUnavailableError on failure/timeout
        after retries. Returns whether the location is serviceable."""
        ...


class CognitoAttributePort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def sync_profile_attributes(self, cognito_sub: str, name: str, default_pincode: str) -> None:
        ...

    def get_mobile_by_sub(self, cognito_sub: str) -> str | None: ...


class OutboxEventPublisherPort(Protocol):
    """Used only by the separate outbox_publisher_handler Lambda — never
    called from the request-handling path (that's the whole point of the
    outbox pattern)."""

    def set_correlation_id(self, correlation_id: str) -> None: ...

    def publish(self, event_type: str, payload: dict, correlation_id: str) -> None: ...
