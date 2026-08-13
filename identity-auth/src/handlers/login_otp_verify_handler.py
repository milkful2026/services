"""POST /v1/auth/login/otp/verify — thin Lambda entrypoint (MA-21 FR-2).

Unlike registration's otp_verify_handler, this calls `issue_tokens`
directly (no AdminCreateUser/AdminConfirmSignUp — those are registration-
only operations) since the user must already exist and be verified to
have reached this point via login_otp_send_handler's gate. Response has
no `isNewUser` field at all (always false by construction in this path —
the spec omits the field rather than hardcoding it, to avoid clients
branching on a value that can't vary here).

Note: OTP verification itself doesn't check that the record's `purpose`
is "LOGIN" specifically (verify_otp is purpose-agnostic — purpose only
drives rate-limit/lock key isolation and SMS template selection, not an
authorization boundary). A REGISTER-purpose OTP consumed via this
endpoint still required actual possession of the code sent to that
phone, so this isn't a security gap — just a design note worth being
explicit about rather than silent on.
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from adapters.cognito_adapter import CognitoAdapter
from adapters.otp_store_adapter import DynamoDbOtpStoreAdapter
from config.env import get_settings
from domain.exceptions import IdentityAuthError
from domain.otp_service import OtpService
from handlers.dto import (
    OtpVerifyRequest,
    error_response,
    success_response,
    validation_error_response,
)

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    otp_store = DynamoDbOtpStoreAdapter(
        settings.otp_requests_table_name, settings.aws_region, endpoint_url=settings.aws_endpoint_url
    )
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id,
        settings.cognito_client_id,
        settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
    )
    # No rate_limiter: verify_otp()/mark_otp_consumed() never call
    # request_otp(), so there's no reason to pay for a Redis connection
    # (and a Redis-outage failure mode) on this code path.
    otp_service = OtpService(
        otp_store=otp_store,
        otp_length=settings.otp_length,
        ttl_seconds=settings.otp_ttl_seconds,
        resend_after_seconds=settings.otp_resend_after_seconds,
        max_attempts=settings.otp_max_attempts,
    )

    _deps = {"cognito": cognito, "otp_service": otp_service}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = OtpVerifyRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        deps["otp_service"].verify_otp(request.mobile, request.otp, request.request_id)
        tokens = deps["cognito"].issue_tokens(request.mobile)
        # Only marked consumed once tokens actually issued — a transient
        # Cognito failure above must leave the OTP retryable rather than
        # burning it for a login that never completed. See
        # OtpService.verify_otp's docstring.
        deps["otp_service"].mark_otp_consumed(request.request_id)

        return success_response(
            {
                "accessToken": tokens.access_token,
                "refreshToken": tokens.refresh_token,
                "expiresIn": tokens.expires_in,
            }
        )
    except IdentityAuthError as exc:
        logger.info(
            "login_otp_verify rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
