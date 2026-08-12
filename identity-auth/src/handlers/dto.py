"""Request/response DTOs and the shared response-envelope helpers.

Per services/README.md: DTOs are pydantic here at the handler boundary
only — domain stays framework-free. The response envelope and event
envelope shapes are fixed by §5 of that doc.
"""

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from domain.exceptions import IdentityAuthError, ValidationError

_MOBILE_PATTERN = re.compile(r"^\+91\d{10}$")


def validate_mobile(value: str) -> str:
    if not _MOBILE_PATTERN.match(value):
        raise ValueError("mobile must be E.164 +91 followed by 10 digits")
    return value


class OtpSendRequest(BaseModel):
    mobile: str

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, v: str) -> str:
        return validate_mobile(v)


class OtpVerifyRequest(BaseModel):
    mobile: str
    otp: str = Field(min_length=4, max_length=8)
    request_id: str = Field(alias="requestId")

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, v: str) -> str:
        return validate_mobile(v)

    model_config = {"populate_by_name": True}


class SocialAuthRequest(BaseModel):
    provider: str
    id_token: str = Field(alias="idToken")

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        if v not in ("google", "apple"):
            raise ValueError("provider must be 'google' or 'apple'")
        return v

    model_config = {"populate_by_name": True}


class TokenRefreshRequest(BaseModel):
    refresh_token: str = Field(alias="refreshToken")

    model_config = {"populate_by_name": True}


def success_response(data: dict[str, Any], status_code: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"requestId": str(uuid.uuid4()), "status": "success", "data": data}),
    }


def error_response(exc: IdentityAuthError) -> dict[str, Any]:
    return {
        "statusCode": exc.http_status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "requestId": str(uuid.uuid4()),
                "status": "error",
                "data": {"errorCode": exc.error_code, "message": exc.message, **exc.details},
            }
        ),
    }


def validation_error_response(message: str) -> dict[str, Any]:
    return error_response(ValidationError(message))
