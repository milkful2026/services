"""POST /v1/auth/otp/send — thin Lambda entrypoint.

Per services/README.md §3.2: routing, DTO mapping, dependency wiring,
exception translation only — no business rules live here. Adapters are
built lazily on first invocation and cached module-globally so they're
reused across warm invocations within one Lambda execution environment,
without requiring env vars to be present at import time (which would
otherwise break test collection — see config.env.get_settings's docstring).
"""

import json
import logging
import uuid

from pydantic import ValidationError as PydanticValidationError

from adapters.cognito_adapter import CognitoAdapter
from adapters.notification_publisher import EventBridgeNotificationPublisher
from adapters.otp_store_adapter import DynamoDbOtpStoreAdapter
from adapters.rate_limit_adapter import (
    RedisLockAdapter,
    RedisRateLimiterAdapter,
    build_redis_client,
)
from config.env import get_settings
from domain.exceptions import IdentityAuthError, NotificationPublishError, UserExistsError
from domain.otp_service import OtpService
from handlers.dto import OtpSendRequest, error_response, success_response, validation_error_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    otp_store = DynamoDbOtpStoreAdapter(settings.otp_requests_table_name, settings.aws_region)
    redis_client = build_redis_client(
        settings.redis_host, settings.redis_port, settings.redis_use_tls
    )
    rate_limiter = RedisRateLimiterAdapter(redis_client)
    send_lock = RedisLockAdapter(redis_client)
    cognito = CognitoAdapter(
        settings.cognito_user_pool_id, settings.cognito_client_id, settings.aws_region
    )
    publisher = EventBridgeNotificationPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )
    otp_service = OtpService(
        otp_store=otp_store,
        rate_limiter=rate_limiter,
        otp_length=settings.otp_length,
        ttl_seconds=settings.otp_ttl_seconds,
        resend_after_seconds=settings.otp_resend_after_seconds,
        max_attempts=settings.otp_max_attempts,
        rate_limit_max_requests=settings.otp_rate_limit_max_requests,
        rate_limit_window_seconds=settings.otp_rate_limit_window_seconds,
        send_lock=send_lock,
    )

    _deps = {
        "settings": settings,
        "cognito": cognito,
        "publisher": publisher,
        "otp_service": otp_service,
    }
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    try:
        body = json.loads(event.get("body") or "{}")
        request = OtpSendRequest.model_validate(body)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        return validation_error_response(str(exc))

    try:
        if deps["cognito"].find_verified_sub_by_phone(request.mobile) is not None:
            raise UserExistsError()

        record, plaintext_otp, is_resend = deps["otp_service"].request_otp(request.mobile)
        if not is_resend:
            try:
                deps["publisher"].publish_otp_requested(
                    request.mobile, plaintext_otp, correlation_id
                )
            except NotificationPublishError:
                # The OTP record is already persisted as ACTIVE, but no SMS
                # was ever delivered — move it out of ACTIVE so the next
                # send attempt isn't swallowed by the resend cooldown.
                deps["otp_service"].mark_send_failed(record.request_id)
                raise

        return success_response(
            {
                "requestId": record.request_id,
                "expiresIn": deps["settings"].otp_ttl_seconds,
                "resendAfter": deps["settings"].otp_resend_after_seconds,
            }
        )
    except IdentityAuthError as exc:
        logger.info(
            "otp_send rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
