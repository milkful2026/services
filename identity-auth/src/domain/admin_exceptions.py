"""Typed domain exceptions for the Admin Identity/RBAC feature (MA-129).

Extends domain.exceptions.IdentityAuthError (the shared base + response-
envelope contract, see that module's docstring) rather than duplicating
it. Kept in a separate module so the pre-existing consumer-flow
exceptions file is never touched by this additive feature.

Per services/README.md §5c's error table, new endpoints use 422 for
domain validation (the pre-existing consumer endpoints in
domain/exceptions.py use 400 for their ValidationError — an
inherited inconsistency, not introduced here; see this service's
README "Architecture decisions flagged for review").
"""

from typing import Any

from domain.exceptions import IdentityAuthError


class AdminValidationError(IdentityAuthError):
    error_code = "VALIDATION_ERROR"
    http_status = 422


class InvalidRoleError(AdminValidationError):
    error_code = "INVALID_ROLE"


class InvalidCidrError(AdminValidationError):
    error_code = "INVALID_CIDR"


class IncorrectAdminCredentialsError(IdentityAuthError):
    """Generic, enumeration-safe 401 for FR-1 — used both when the email
    doesn't exist in the Admin Pool at all and when the password is
    wrong for a real admin. Message text is fixed by spec FR-1."""

    error_code = "INCORRECT_CREDENTIALS"
    http_status = 401

    def __init__(self, message: str = "Incorrect email or password") -> None:
        super().__init__(message)


class AdminAccountPendingError(IdentityAuthError):
    error_code = "ADMIN_ACCOUNT_PENDING"
    http_status = 403

    def __init__(self, message: str = "This account is pending activation") -> None:
        super().__init__(message)


class AdminAccountDeactivatedError(IdentityAuthError):
    error_code = "ADMIN_ACCOUNT_DEACTIVATED"
    http_status = 403

    def __init__(self, message: str = "This account has been deactivated") -> None:
        super().__init__(message)


class ChallengeExpiredError(IdentityAuthError):
    """Distinct from a wrong 2FA code (Invalid2faCodeError) — spec §9
    edge case: an expired/unknown challengeToken must prompt the UI to
    restart from the password step, with its own error code."""

    error_code = "CHALLENGE_EXPIRED"
    http_status = 401

    def __init__(self, message: str = "Challenge has expired, please log in again") -> None:
        super().__init__(message)


class Invalid2faCodeError(IdentityAuthError):
    error_code = "INVALID_2FA_CODE"
    http_status = 401

    def __init__(self, message: str = "Incorrect verification code") -> None:
        super().__init__(message)


class AdminAccountLockedError(IdentityAuthError):
    error_code = "ADMIN_ACCOUNT_LOCKED"
    http_status = 401

    def __init__(self, message: str = "Account temporarily locked due to too many failed attempts") -> None:
        super().__init__(message)


class AdminEmailExistsError(IdentityAuthError):
    error_code = "ADMIN_EMAIL_EXISTS"
    http_status = 409

    def __init__(self, message: str = "An admin with this email already exists") -> None:
        super().__init__(message)


class AdminNotFoundError(IdentityAuthError):
    error_code = "ADMIN_NOT_FOUND"
    http_status = 404

    def __init__(self, message: str = "Admin user not found") -> None:
        super().__init__(message)


class SelfDeactivationError(IdentityAuthError):
    error_code = "SELF_DEACTIVATION_NOT_ALLOWED"
    http_status = 400

    def __init__(self, message: str = "You cannot deactivate your own account") -> None:
        super().__init__(message)


class AdminForbiddenError(IdentityAuthError):
    """Non-SuperAdmin caller on a SuperAdmin-only endpoint, or an
    authorizer-level denial surfaced to a handler (defense in depth —
    the authorizer is the primary enforcement point per FR-3/FR-4)."""

    error_code = "FORBIDDEN"
    http_status = 403

    def __init__(self, message: str = "You do not have permission to perform this action") -> None:
        super().__init__(message)


class AdminAuthenticationError(IdentityAuthError):
    """Missing/malformed caller identity where one is required (e.g. no
    authorizer context reached the handler)."""

    error_code = "UNAUTHENTICATED"
    http_status = 401

    def __init__(self, message: str = "Authentication required", details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details)
