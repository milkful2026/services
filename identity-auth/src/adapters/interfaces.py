"""Abstract adapter interfaces (Protocols).

Per services/README.md §3.7: adapter interfaces are abstract; implementations
are wired at the composition root (each handler module). Domain code depends
on these Protocols only, never on boto3/redis/requests directly.
"""

from typing import Protocol

from domain.models import OtpRecord, TokenBundle


class OtpStorePort(Protocol):
    def put(self, record: OtpRecord) -> None: ...
    def get(self, request_id: str) -> OtpRecord | None: ...
    def get_active_by_mobile(self, mobile: str, purpose: str) -> OtpRecord | None:
        """Scoped by purpose ("REGISTER" | "LOGIN") — a mobile with an
        active registration OTP and a login attempt for that same mobile
        must never be conflated into one "duplicate send" (spec MA-21)."""
        ...
    def increment_attempts(self, request_id: str) -> int: ...
    def mark_status(self, request_id: str, status: str) -> None: ...


class RateLimiterPort(Protocol):
    def check_and_increment(self, key: str, max_requests: int, window_seconds: int) -> None:
        """Raises RateLimitExceededError if the limit is already exhausted."""
        ...


class OtpSendLockPort(Protocol):
    def acquire(self, key: str, ttl_seconds: int) -> bool:
        """Attempts to acquire a short-lived mutex. Returns True if acquired,
        False if another caller currently holds it."""
        ...

    def release(self, key: str) -> None: ...


class CognitoPort(Protocol):
    def find_verified_sub_by_phone(self, mobile: str) -> str | None:
        """Returns the Cognito `sub` if a phone_number_verified user exists, else None."""
        ...

    def register_and_issue_tokens(self, mobile: str) -> tuple[TokenBundle, bool]:
        """Create-or-confirm the Cognito user for `mobile`, mark phone_number_verified,
        and issue tokens. Returns (tokens, is_new_user).

        Username == mobile in this pool (phone is the username attribute), so no
        separate username lookup is needed for the immediate token issuance that
        follows — the password set here is single-use and never persisted.
        """
        ...

    def find_or_create_federated_user(
        self, provider: str, provider_sub: str, email: str
    ) -> tuple[str, str | None, bool, bool]:
        """Returns (sub, mobile_or_none, is_new_user, mobile_verified).
        `email` is required — this pool has no username-eligible identifier
        for a federated user without one (see cognito_adapter's docstring).
        """
        ...

    def issue_tokens(self, username: str) -> TokenBundle:
        """Issues fresh tokens for an already-created/linked user, keyed by
        Cognito Username (== mobile in this pool), not `sub`."""
        ...

    def refresh_tokens(self, refresh_token: str) -> TokenBundle: ...

    def revoke_token(self, refresh_token: str) -> None:
        """Per-device logout (spec MA-21 FR-3) — revokes only the given
        refresh token, never every session for the user."""
        ...


class SocialTokenVerifierPort(Protocol):
    def verify(self, provider: str, id_token: str) -> dict:
        """Returns verified claims (sub, email, iss, aud, exp, ...)."""
        ...


class EventPublisherPort(Protocol):
    def publish_otp_requested(
        self, mobile: str, otp: str, correlation_id: str, purpose: str = "REGISTER"
    ) -> None:
        """`purpose` selects the SMS template (spec MA-21 FR-1:
        template: "login" vs the registration default)."""
        ...
