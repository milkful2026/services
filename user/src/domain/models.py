"""Domain models. Plain dataclasses only — no SQLAlchemy/pydantic types."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


@dataclass
class Address:
    lines: list[str]
    city: str
    state: str
    pincode: str
    lat: float
    lng: float
    landmark: str | None = None
    is_default: bool = False
    zone_id: str | None = None
    id: str | None = None  # assigned on insert


@dataclass
class ServiceabilityResult:
    """Inventory's own answer for a pincode/lat/lng — `zone_id` (None
    when not serviceable) is the authoritative zone for that location.
    Never substitute a client-supplied zoneId for this: the client's
    value is never cross-validated against the coordinates that were
    actually checked."""

    serviceable: bool
    zone_id: str | None = None


@dataclass
class Consent:
    type: str  # "TERMS" | "PRIVACY" | "PUSH_NOTIFICATIONS"
    accepted_at: str  # ISO-8601
    version: str | None = None
    accepted: bool = True


@dataclass
class RegistrationRequest:
    cognito_sub: str
    mobile: str  # from JWT claims, never the request body — see registration_service
    name: str
    addresses: list[Address]
    consents: list[Consent]
    email: str | None = None
    preferred_slot_id: str | None = None


@dataclass
class RegistrationResult:
    user_id: str
    default_address_id: str
    is_new_user: bool
    wallet_id: str | None = None
    wallet_status: str = "PENDING"


@dataclass
class DeliverySlot:
    id: str
    label: str
    available: bool = True


@dataclass
class UserProfile:
    user_id: str
    name: str
    mobile: str
    account_type: str  # "B2C" | "B2B" — always "B2C" until a B2B onboarding path exists
    default_address_id: str
    default_address_state: str | None = None  # None when no default address is set
    # None when no default address, or it predates zone_id
    default_address_zone_id: str | None = None
    # MA-135 FR-6 — the whole default address as saved from the onboarding
    # Google Maps / Places screen; None when no default address is set.
    default_address: Address | None = None


# --- MA-139: Customer Account Status --------------------------------------


class CustomerStatus(StrEnum):
    """Mirrors identity-auth's `domain/admin_models.py::AdminStatus` style
    (StrEnum, values matching the wire/DB strings exactly) — MA-139 §7."""

    ACTIVE = "Active"
    SUSPENDED = "Suspended"
    DEACTIVATED = "Deactivated"


@dataclass
class UserStatusHistoryEntry:
    """One row of `user_status_history` (MA-139 §7) — the audit trail
    entry for a single status transition."""

    id: str
    user_id: str
    previous_status: str | None
    new_status: str
    reason: str | None
    effective_from: date | None
    actor_admin_id: str
    created_at: datetime | None = None


@dataclass
class CustomerAccount:
    """MA-139 §4 FR-1/FR-2 — an admin-facing customer account summary
    (list) or detail (with `status_history` populated) row. `id` is this
    service's own `users.id`, not the Cognito sub."""

    id: str
    name: str
    mobile: str
    email: str | None
    account_type: str
    status: str
    status_reason: str | None
    # The most recent user_status_history.created_at for this account —
    # deliberately NOT status_effective_from (only a DATE, loses
    # time-of-day precision), per spec §4 FR-1. None if the account has
    # never had a status change.
    last_status_change_at: datetime | None
    cognito_sub: str = ""
    suspended_until: date | None = None
    status_history: list[UserStatusHistoryEntry] = field(default_factory=list)


@dataclass
class CustomerPage:
    items: list[CustomerAccount]
    total: int
    page: int
    page_size: int


@dataclass
class BulkStatusResult:
    customer_id: str
    success: bool
    error_code: str | None = None
