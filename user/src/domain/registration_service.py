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
from domain.exceptions import ValidationError
from domain.models import Address, Consent, DeliverySlot, RegistrationRequest, RegistrationResult

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

    def register(self, request: RegistrationRequest) -> RegistrationResult:
        _validate(request)
        default_address = _default_address(request.addresses)

        # Must complete and pass BEFORE the DB transaction — a
        # non-serviceable address must never reach the database.
        self._inventory_client.check_serviceability(
            default_address.pincode, default_address.lat, default_address.lng
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

        # Best-effort, after the DB transaction already committed: the
        # registration itself (user/address/consent persisted, outbox
        # written) is the thing that must not fail here. A Cognito sync
        # failure is logged, not raised — raising would give the client a
        # false 500 for a registration that actually succeeded.
        try:
            self._cognito_attributes.sync_profile_attributes(
                request.cognito_sub, request.name, default_address.pincode
            )
        except Exception:
            logger.error(
                "cognito profile sync failed post-registration (non-fatal)",
                extra={"correlationId": self._correlation_id, "userId": result.user_id},
            )

        return result

    def get_delivery_slots(self, zone_id: str) -> list[DeliverySlot]:
        return self._user_repository.get_delivery_slots(zone_id)


def _validate(request: RegistrationRequest) -> None:
    if not (2 <= len(request.name) <= 100):
        raise ValidationError("name must be 2-100 characters")

    if len(request.addresses) != 1:
        raise ValidationError("registration requires exactly 1 address")

    address = request.addresses[0]
    if not address.pincode.isdigit() or len(address.pincode) != 6:
        raise ValidationError(f"Invalid pincode format: {address.pincode!r}")

    consent_types = {c.type for c in request.consents}
    missing = _MANDATORY_CONSENT_TYPES - consent_types
    if missing:
        raise ValidationError(f"Missing mandatory consents: {sorted(missing)}")


def _default_address(addresses: list[Address]) -> Address:
    defaults = [a for a in addresses if a.is_default]
    if len(defaults) != 1:
        raise ValidationError("exactly one address must have isDefault=true")
    return defaults[0]
