"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/requests/boto3 directly."""

from typing import Protocol

from domain.models import Address, Consent, DeliverySlot, RegistrationResult


class UserRepositoryPort(Protocol):
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
        """Single DB transaction: upsert `users` (idempotent on
        cognito_sub — a duplicate call returns the existing row with
        is_new_user=False and writes nothing else), insert addresses,
        insert consents, insert one outbox_events row. Per spec §9,
        the outbox write happens in the SAME transaction as everything
        else, so "event publish fails" can never mean "user wasn't
        created" or vice versa.
        """
        ...

    def get_delivery_slots(self, zone_id: str) -> list[DeliverySlot]:
        """Reads from this service's own zone_slots reference table —
        see registration_service.py's module docstring for why this
        isn't a live Inventory call."""
        ...

    def get_unpublished_outbox_events(self, limit: int) -> list[dict]:
        """Used only by outbox_publisher_handler, not the request path."""
        ...

    def mark_outbox_published(self, event_id: str) -> None: ...


class InventoryClientPort(Protocol):
    def check_serviceability(self, pincode: str, lat: float, lng: float) -> bool:
        """Raises ExternalServiceUnavailableError on failure/timeout
        after retries. Returns whether the location is serviceable."""
        ...


class CognitoAttributePort(Protocol):
    def sync_profile_attributes(self, cognito_sub: str, name: str, default_pincode: str) -> None:
        ...


class OutboxEventPublisherPort(Protocol):
    """Used only by the separate outbox_publisher_handler Lambda — never
    called from the request-handling path (that's the whole point of the
    outbox pattern)."""

    def publish(self, event_type: str, payload: dict, correlation_id: str) -> None: ...
