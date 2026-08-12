"""POST /v1/auth/social — thin Lambda entrypoint (FR-3)."""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from adapters.cognito_adapter import CognitoAdapter
from adapters.social_jwks_adapter import SocialJwksAdapter
from config.env import get_settings
from domain.exceptions import IdentityAuthError
from domain.social_link_service import SocialLinkService
from handlers.dto import SocialAuthRequest, error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id, settings.cognito_client_id, settings.aws_region
    )
    token_verifier = SocialJwksAdapter(
        google_client_id=settings.google_client_id,
        apple_client_id=settings.apple_client_id,
        google_jwks_url=settings.google_jwks_url,
        apple_jwks_url=settings.apple_jwks_url,
        cache_ttl_seconds=settings.jwks_cache_ttl_seconds,
    )
    social_link_service = SocialLinkService(token_verifier, cognito)

    _deps = {"social_link_service": social_link_service}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = SocialAuthRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        result = deps["social_link_service"].authenticate(request.provider, request.id_token)

        if result.requires_mobile_verification:
            return success_response(
                {
                    "requiresMobileVerification": True,
                    "partialToken": result.partial_token,
                }
            )

        return success_response(
            {
                "accessToken": result.tokens.access_token,
                "refreshToken": result.tokens.refresh_token,
                "expiresIn": result.tokens.expires_in,
                "isNewUser": result.is_new_user,
            }
        )
    except IdentityAuthError as exc:
        logger.info(
            "social_auth rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
