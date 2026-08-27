"""Registration orchestration (FR-1) and delivery slots (FR-2).

**Delivery slots deviate from the original plan.** Inventory's actual API
(built in MA-95) only accepts pincode/lat/lng and returns slots as part of
a serviceability-check response — there is no zoneId-keyed "get slots"
endpoint to call. Since the spec explicitly offers "cached from Inventory
or local replica" as the two options for this FR, and a live call isn't
possible against Inventory's real contract, `get_delivery_slots` reads
from this service's own `zone_slots` reference table instead. How that
table gets populated/kept in sync with Inventory's zone data is not wired
here — flagged as a real gap needing a human decision (poll Inventory
periodically? Consume a zone-sync event? Manual seed data?), not invented.
"""

import logging

from adapters.interfaces import (
    CognitoAttributePort,
    InventoryClientPort,
    UserRepositoryPort,
)
from domain.exceptions import (
    ExternalServiceUnavailableError,
    NotServiceableError,
    UserNotFoundError,
    ValidationError,
)
from domain.models import (
    Address,
    DeliverySlot,
    RegistrationRequest,
    RegistrationResult,
    UserProfile,
)

logger = logging.getLogger(__name__)

_MANDATORY_CONSENT_TYPES = {"TERMS", "PRIVACY"}


class RegistrationService:
    def __init__(
        self,
        user_repository: UserRepositoryPort,
        inventory_client: InventoryClientPort,
        cognito_attributes: CognitoAttributePort,
        correlation_id: str = "",
    ) -> None:
        self._user_repository = user_repository
        self._inventory_client = inventory_client
        self._cognito_attributes = cognito_attributes
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        """Cascades a per-request correlation ID to every collaborator —
        this service and its adapters are constructed once and reused
        across warm Lambda invocations, so the ID can't be baked in at
        construction time."""
        self._correlation_id = correlation_id
        self._user_repository.set_correlation_id(correlation_id)
        self._inventory_client.set_correlation_id(correlation_id)
        self._cognito_attributes.set_correlation_id(correlation_id)

    def resolve_mobile(self, cognito_sub: str) -> str:
        """Server-side lookup, not a JWT claim — see
        cognito_attribute_adapter.py's module docstring for why: the spec
        authorizes /users/register with a Cognito access token, and
        access tokens never carry phone_number."""
        mobile = self._cognito_attributes.get_mobile_by_sub(cognito_sub)
        if mobile is None:
            raise ExternalServiceUnavailableError(
                "Could not resolve this account's mobile number"
            )
        return mobile

    def register(self, request: RegistrationRequest) -> RegistrationResult:
        _validate(request)
        default_address = _default_address(request.addresses)

        # Cheap indexed lookup first — a duplicate/retried registration
        # for an already-registered cognito_sub must not pay for an
        # Inventory round-trip (with retries) or re-sync Cognito with
        # this request's (possibly different, never-persisted) address.
        existing = self._user_repository.get_by_cognito_sub(request.cognito_sub)
        if existing is not None:
            return existing

        # Must complete and pass BEFORE the DB transaction — a
        # non-serviceable address must never reach the database.
        serviceable = self._inventory_client.check_serviceability(
            default_address.pincode, default_address.lat, default_address.lng
        )
        if not serviceable:
            raise NotServiceableError(
                f"Address with pincode {default_address.pincode!r} is not serviceable"
            )

        result = self._user_repository.register(
            cognito_sub=request.cognito_sub,
            mobile=request.mobile,
            name=request.name,
            email=request.email,
            addresses=request.addresses,
            preferred_slot_id=request.preferred_slot_id,
            consents=request.consents,
            outbox_event_type="UserRegistered",
            outbox_payload={
                "cognitoSub": request.cognito_sub,
                "mobile": request.mobile,
                "defaultPincode": default_address.pincode,
            },
        )

        if not result.is_new_user:
            # Lost a concurrent-registration race (see
            # user_repository.register's IntegrityError handling) — the
            # winning call already synced Cognito with its own address;
            # this request's data was never persisted, so it must not be
            # synced either.
            return result

        # Best-effort, after the DB transaction already committed: the
        # registration itself (user/address/consent persisted, outbox
        # written) is the thing that must not fail here. A genuine
        # external-service failure is logged, not raised — raising would
        # give the client a false 500 for a registration that actually
        # succeeded. Anything else (a bug in the adapter) is left to
        # propagate loudly rather than being masked as a routine sync
        # failure.
        try:
            self._cognito_attributes.sync_profile_attributes(
                request.cognito_sub, request.name, default_address.pincode
            )
        except ExternalServiceUnavailableError:
            logger.error(
                "cognito profile sync failed post-registration (non-fatal)",
                extra={"correlationId": self._correlation_id, "userId": result.user_id},
            )

        return result

    def get_delivery_slots(self, zone_id: str) -> list[DeliverySlot]:
        return self._user_repository.get_delivery_slots(zone_id)

    def get_my_profile(self, cognito_sub: str) -> UserProfile:
        profile = self._user_repository.get_profile_by_sub(cognito_sub)
        if profile is None:
            raise UserNotFoundError("No profile found for this account")
        return profile


def _validate(request: RegistrationRequest) -> None:
    if not (2 <= len(request.name) <= 100):
        raise ValidationError("name must be 2-100 characters")

    if len(request.addresses) != 1:
        raise ValidationError("registration requires exactly 1 address")

    address = request.addresses[0]
    if not address.pincode.isdigit() or len(address.pincode) != 6:
        raise ValidationError(f"Invalid pincode format: {address.pincode!r}")

    accepted_consent_types = {c.type for c in request.consents if c.accepted}
    missing = _MANDATORY_CONSENT_TYPES - accepted_consent_types
    if missing:
        raise ValidationError(f"Missing mandatory consents: {sorted(missing)}")


def _default_address(addresses: list[Address]) -> Address:
    defaults = [a for a in addresses if a.is_default]
    if len(defaults) != 1:
        raise ValidationError("exactly one address must have isDefault=true")
    return defaults[0]
