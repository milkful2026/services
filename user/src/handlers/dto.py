"""Request/response DTOs and response-envelope helpers. Fixed envelope
shape per services/README.md §5."""

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

from domain.exceptions import UserServiceError, ValidationError
from domain.models import Address, Consent, DeliverySlot, RegistrationRequest, RegistrationResult


class AddressDto(BaseModel):
    lines: list[str]
    city: str
    state: str
    pincode: str
    lat: float
    lng: float
    landmark: str | None = None
    is_default: bool = Field(alias="isDefault", default=False)

    model_config = {"populate_by_name": True}


class ConsentDto(BaseModel):
    type: str
    version: str | None = None
    accepted: bool = True
    accepted_at: str = Field(alias="acceptedAt")

    model_config = {"populate_by_name": True}


class RegisterRequestDto(BaseModel):
    name: str
    email: str | None = None
    addresses: list[AddressDto]
    preferred_slot_id: str | None = Field(alias="preferredSlotId", default=None)
    consents: list[ConsentDto]

    model_config = {"populate_by_name": True}

    def to_domain(self, cognito_sub: str, mobile: str) -> RegistrationRequest:
        return RegistrationRequest(
            cognito_sub=cognito_sub,
            mobile=mobile,
            name=self.name,
            email=self.email,
            addresses=[
                Address(
                    lines=a.lines,
                    city=a.city,
                    state=a.state,
                    pincode=a.pincode,
                    lat=a.lat,
                    lng=a.lng,
                    landmark=a.landmark,
                    is_default=a.is_default,
                )
                for a in self.addresses
            ],
            preferred_slot_id=self.preferred_slot_id,
            consents=[
                Consent(
                    type=c.type, version=c.version, accepted=c.accepted, accepted_at=c.accepted_at
                )
                for c in self.consents
            ],
        )


def serialize_registration_result(result: RegistrationResult) -> dict[str, Any]:
    return {
        "userId": result.user_id,
        "walletId": result.wallet_id,
        "walletStatus": result.wallet_status,
        "defaultAddressId": result.default_address_id,
    }


def serialize_delivery_slots(slots: list[DeliverySlot]) -> list[dict[str, Any]]:
    return [{"id": s.id, "label": s.label, "available": s.available} for s in slots]


def success_response(data: Any, status_code: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"requestId": str(uuid.uuid4()), "status": "success", "data": data}),
    }


def error_response(exc: UserServiceError) -> dict[str, Any]:
    # `details` is spread AFTER the canonical keys are computed and
    # filtered, not into them — an upstream error payload forwarded as
    # `details` (e.g. Inventory's own {errorCode, message, ...}) must
    # never silently overwrite this service's own error_code/message.
    safe_details = {k: v for k, v in exc.details.items() if k not in ("errorCode", "message")}
    return {
        "statusCode": exc.http_status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "requestId": str(uuid.uuid4()),
                "status": "error",
                "data": {"errorCode": exc.error_code, "message": exc.message, **safe_details},
            }
        ),
    }


def validation_error_response(message: str) -> dict[str, Any]:
    return error_response(ValidationError(message))
