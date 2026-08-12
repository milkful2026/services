"""Domain models. Plain dataclasses only — no SQLAlchemy/pydantic types."""

from dataclasses import dataclass, field


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
    id: str | None = None  # assigned on insert


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
