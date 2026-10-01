"""Typed domain exceptions. Every exception carries a stable `error_code`
and HTTP status handlers map it to — never a raw traceback.

Duplicate registration is NOT modeled as an exception — per spec §8 it's
an idempotent 200 with the existing userId, handled as a normal return
value (RegistrationResult.is_new_user=False), not an error path.
"""

from typing import Any


class UserServiceError(Exception):
    error_code: str = "USER_SERVICE_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(UserServiceError):
    error_code = "VALIDATION_ERROR"
    http_status = 400


class NotServiceableError(UserServiceError):
    error_code = "NOT_SERVICEABLE"
    http_status = 422


class UserNotFoundError(UserServiceError):
    """JWT `sub` has no matching `users` row (spec MA-107 FR-2) — should
    not happen in practice post-MA-1, but must be a clean 404, not a 500,
    per that spec's own edge-case requirement."""

    error_code = "USER_NOT_FOUND"
    http_status = 404


class ExternalServiceUnavailableError(UserServiceError):
    """Cognito, Inventory, or the DB unreachable after retries."""

    error_code = "EXTERNAL_SERVICE_UNAVAILABLE"
    http_status = 503


# --- MA-139: Customer Account Status --------------------------------------


class CustomerNotFoundError(UserServiceError):
    """No `users` row for the given admin-supplied customer id (MA-139
    §4 FR-2) — a clean 404, not a 500."""

    error_code = "CUSTOMER_NOT_FOUND"
    http_status = 404


class InvalidStatusTransitionError(UserServiceError):
    """Suspend attempted on an already-`Deactivated` account (MA-139 §4
    FR-3/§9) — must be reactivated first, not silently downgraded."""

    error_code = "INVALID_STATUS_TRANSITION"
    http_status = 409


class CognitoSyncFailedError(UserServiceError):
    """MA-139 §6/§11 — the `users`/`user_status_history`/outbox DB
    transaction already committed before this is raised; only the
    synchronous `AdminDisableUser`/`AdminEnableUser` call failed. A
    distinct 502 (not the generic 503 ExternalServiceUnavailableError)
    per spec §6's explicit compensating-retry contract: the DB state is
    already correct and safe to retry (FR-3/FR-4 are idempotent on
    status), so the caller should treat this as "retry the same action",
    not as "nothing happened"."""

    error_code = "COGNITO_SYNC_FAILED"
    http_status = 502
