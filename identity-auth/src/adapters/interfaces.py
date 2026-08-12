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
    def get_active_by_mobile(self, mobile: str) -> OtpRecord | None: ...
    def increment_attempts(self, request_id: str) -> int: ...
    def mark_status(self, request_id: str, status: str) -> None: ...


class RateLimiterPort(Protocol):
    def check_and_increment(self, key: str, max_requests: int, window_seconds: int) -> None:
        """Raises RateLimitExceededError if the limit is already exhausted."""
        ...


class CognitoPort(Protocol):
    def find_verified_user_by_phone(self, mobile: str) -> str | None:
        """Returns the Cognito `sub` if a phone_number_verified user exists, else None."""
        ...

    def create_or_confirm_user(self, mobile: str) -> tuple[str, bool]:
        """Returns (sub, is_new_user)."""
        ...

    def issue_tokens_for_sub(self, sub: str) -> TokenBundle: ...

    def find_or_create_federated_user(
        self, provider: str, provider_sub: str, email: str | None
    ) -> tuple[str, bool, bool]:
        """Returns (sub, is_new_user, mobile_verified)."""
        ...

    def refresh_tokens(self, refresh_token: str) -> TokenBundle: ...


class SocialTokenVerifierPort(Protocol):
    def verify(self, provider: str, id_token: str) -> dict:
        """Returns verified claims (sub, email, iss, aud, exp, ...)."""
        ...


class EventPublisherPort(Protocol):
    def publish_otp_requested(self, mobile: str, otp: str, correlation_id: str) -> None: ...
