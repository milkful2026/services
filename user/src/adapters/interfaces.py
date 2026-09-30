"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/requests/boto3 directly."""

from datetime import date
from typing import Protocol

from domain.models import (
    Address,
    Consent,
    CustomerAccount,
    CustomerPage,
    DeliverySlot,
    RegistrationResult,
    ServiceabilityResult,
    UserProfile,
    UserStatusHistoryEntry,
)


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

    # --- MA-139: Customer Account Status ---

    def list_customers(
        self,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> CustomerPage:
        """Spec §4 FR-1 — paginated, `status`-filtered, free-text search
        over name/mobile/email. `lastStatusChangeAt` on each row is the
        most recent `user_status_history.created_at`, not
        `status_effective_from` — see CustomerAccount's own docstring."""
        ...

    def get_customer_by_id(self, customer_id: str) -> CustomerAccount | None:
        """Spec §4 FR-2 — profile only, `status_history` left empty; the
        caller (customer_status_service) populates it via
        get_status_history separately so list-style reads never pay for
        the join."""
        ...

    def get_status_history(self, customer_id: str) -> list[UserStatusHistoryEntry]:
        """Newest first (spec §4 FR-2)."""
        ...

    def update_customer_status(
        self,
        customer_id: str,
        *,
        new_status: str,
        status_reason: str | None,
        status_effective_from: date | None,
        history_effective_from: date | None,
        suspended_until: date | None,
        actor_admin_id: str,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> CustomerAccount:
        """One DB transaction (spec §6/§9): UPDATE users SET status/
        status_reason/status_effective_from/suspended_until, INSERT
        user_status_history, INSERT outbox_events — mirrors register()'s
        own "one transaction, insert row + insert outbox_events row"
        shape. `status_effective_from` (the `users` column — "when did
        the current status take effect") and `history_effective_from`
        (the `user_status_history` row's own `effective_from`) are
        deliberately separate parameters: spec §7 requires the history
        row's `effective_from` to be null specifically for a reactivation
        (`new_status = Active`), even though the `users` column itself is
        still updated to record that the Active status took effect now.
        Returns the updated account (status_history left empty). Raises
        CustomerNotFoundError if no such row exists."""
        ...

    def list_expired_suspensions(self, as_of: date) -> list[CustomerAccount]:
        """Spec section 4 FR-7 sweep candidates: `status = 'Suspended' AND
        suspended_until <= as_of`."""
        ...

    def set_cognito_sync_pending(self, customer_id: str, pending: bool) -> None:
        """Code-review fix (not itself part of MA-139's spec): flips a
        `cognito_sync_pending` flag on the `users` row, set true when a
        status change's DB transaction committed but the synchronous
        AdminDisableUser/AdminEnableUser call then failed
        (CognitoSyncFailedError), cleared once that account's Cognito
        state is next successfully synced. Lets the FR-7 sweep find and
        re-attempt drifted accounts without a human noticing and
        manually retrying the same admin action."""
        ...

    def list_cognito_sync_pending(self) -> list[CustomerAccount]:
        """Every account currently flagged `cognito_sync_pending` --
        candidates for the FR-7 sweep's drift-reconciliation pass,
        regardless of current status (a reactivate's AdminEnableUser can
        drift just as much as a suspend/deactivate's AdminDisableUser
        can)."""
        ...


class InventoryClientPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def check_serviceability(self, pincode: str, lat: float, lng: float) -> ServiceabilityResult:
        """Raises ExternalServiceUnavailableError on failure/timeout
        after retries. Returns whether the location is serviceable, and
        Inventory's own authoritative zone_id for it — the caller must
        use this zone_id, never a client-supplied one, since it's the
        only value actually verified against pincode/lat/lng."""
        ...


class CognitoAttributePort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def sync_profile_attributes(self, cognito_sub: str, name: str, default_pincode: str) -> None:
        ...

    def get_mobile_by_sub(self, cognito_sub: str) -> str | None: ...

    def disable_user(self, cognito_sub: str) -> None:
        """MA-139 §4 FR-3/FR-4 — `AdminDisableUser` against the consumer
        pool (the same pool `sync_profile_attributes`/`get_mobile_by_sub`
        already target). Raises ExternalServiceUnavailableError (mapped
        to 502 by the caller, per spec §6) if the Cognito user for this
        sub no longer exists or the call otherwise fails — never silently
        swallowed (spec §9's orphaned-Cognito-state edge case)."""
        ...

    def enable_user(self, cognito_sub: str) -> None:
        """MA-139 §4 FR-5/FR-7 — `AdminEnableUser` against the consumer
        pool."""
        ...


class OutboxEventPublisherPort(Protocol):
    """Used only by the separate outbox_publisher_handler Lambda — never
    called from the request-handling path (that's the whole point of the
    outbox pattern)."""

    def set_correlation_id(self, correlation_id: str) -> None: ...

    def publish(self, event_type: str, payload: dict, correlation_id: str) -> None: ...
