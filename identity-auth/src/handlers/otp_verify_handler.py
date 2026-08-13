"""POST /v1/auth/otp/verify — thin Lambda entrypoint.

Per spec FR-2: on OTP success, create-or-confirm the Cognito user and
issue tokens. Response shape matches the spec exactly (accessToken,
refreshToken, expiresIn, isNewUser) — idToken is available on the
TokenBundle but intentionally not added to the response since the spec
doesn't ask for it.
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
    otp_store = DynamoDbOtpStoreAdapter(settings.otp_requests_table_name, settings.aws_region)
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id, settings.cognito_client_id, settings.aws_region
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
        tokens, is_new_user = deps["cognito"].register_and_issue_tokens(request.mobile)
        # Only marked consumed once tokens actually issued — a transient
        # Cognito failure above must leave the OTP retryable rather than
        # burning it for a registration that never completed. See
        # OtpService.verify_otp's docstring.
        deps["otp_service"].mark_otp_consumed(request.request_id)

        return success_response(
            {
                "accessToken": tokens.access_token,
                "refreshToken": tokens.refresh_token,
                "expiresIn": tokens.expires_in,
                "isNewUser": is_new_user,
            }
        )
    except IdentityAuthError as exc:
        logger.info(
            "otp_verify rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
