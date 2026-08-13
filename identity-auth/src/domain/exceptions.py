"""Typed domain exceptions.

Per services/README.md §5c and §3.4: domain raises typed exceptions, never
returns null to signal failure. Every exception here carries a stable
`error_code` that handlers place in the response envelope, and an HTTP
status handlers map it to — never a raw traceback.
"""

from typing import Any


class IdentityAuthError(Exception):
    """Base for every domain-level failure in this service."""

    error_code: str = "IDENTITY_AUTH_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(IdentityAuthError):
    error_code = "VALIDATION_ERROR"
    http_status = 400


class UserExistsError(IdentityAuthError):
    """Existing, verified Cognito user — client should redirect to login."""

    error_code = "USER_EXISTS"
    http_status = 409

    def __init__(self, message: str = "User already registered") -> None:
        super().__init__(message, details={"redirect": "login"})


class UserNotFoundError(IdentityAuthError):
    """No Cognito user (or none phone_number_verified) for a login OTP
    send — client should redirect to signup, the mirror-image of
    UserExistsError's redirect-to-login."""

    error_code = "USER_NOT_FOUND"
    http_status = 404

    def __init__(self, message: str = "No verified account for this mobile number") -> None:
        super().__init__(message, details={"redirect": "signup"})


class OtpRequestNotFoundError(IdentityAuthError):
    error_code = "OTP_REQUEST_NOT_FOUND"
    http_status = 400


class OtpExpiredError(IdentityAuthError):
    error_code = "OTP_EXPIRED"
    http_status = 401


class OtpInvalidError(IdentityAuthError):
    error_code = "OTP_INVALID"
    http_status = 401


class OtpAttemptsExceededError(IdentityAuthError):
    error_code = "OTP_ATTEMPTS_EXCEEDED"
    http_status = 401


class RateLimitExceededError(IdentityAuthError):
    error_code = "RATE_LIMIT_EXCEEDED"
    http_status = 429


class OtpRequestInProgressError(IdentityAuthError):
    """A concurrent otp/send request for the same mobile is already being
    processed — the client should retry shortly rather than racing to
    create a second ACTIVE OTP record for the same mobile."""

    error_code = "OTP_REQUEST_IN_PROGRESS"
    http_status = 409


class InvalidSocialTokenError(IdentityAuthError):
    error_code = "INVALID_SOCIAL_TOKEN"
    http_status = 401


class SocialAccountConflictError(IdentityAuthError):
    """Social email already linked to a different mobile number."""

    error_code = "SOCIAL_ACCOUNT_CONFLICT"
    http_status = 409

    def __init__(self, message: str, merge_instruction_code: str) -> None:
        super().__init__(message, details={"mergeInstructionCode": merge_instruction_code})


class InvalidRefreshTokenError(IdentityAuthError):
    error_code = "INVALID_REFRESH_TOKEN"
    http_status = 401


class ExternalServiceUnavailableError(IdentityAuthError):
    """Upstream (Cognito, Redis, JWKS provider) unavailable after retries."""

    error_code = "EXTERNAL_SERVICE_UNAVAILABLE"
    http_status = 503


class NotificationPublishError(IdentityAuthError):
    """EventBridge publish failed after retries — non-fatal to the caller,
    but the caller decides whether to surface it (see FR-1 edge cases)."""

    error_code = "NOTIFICATION_PUBLISH_FAILED"
    http_status = 502
