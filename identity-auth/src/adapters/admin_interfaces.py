"""Abstract adapter interfaces (Protocols) for the Admin Identity/RBAC
feature (MA-129).

Per services/README.md §3.7: adapter interfaces are abstract; implementations
are wired at the composition root (each handler module). Domain code depends
on these Protocols only, never on boto3/SQLAlchemy/redis directly. Kept
separate from adapters/interfaces.py so the pre-existing consumer-flow
Protocols are never touched by this additive feature.
"""

from typing import Protocol

from domain.admin_models import AdminTokenBundle, AdminUser, AdminUserPage, LoginChallenge


class AdminCognitoPort(Protocol):
    def admin_password_auth(self, email: str, password: str) -> str:
        """First factor (FR-1). Returns the Cognito `Session` string for
        the SOFTWARE_TOKEN_MFA challenge that MUST follow, since TOTP MFA
        is required at the Admin Pool level. Raises
        IncorrectAdminCredentialsError on bad credentials (never leaks
        whether the email exists)."""
        ...

    def respond_to_mfa_challenge(self, email: str, session: str, code: str) -> AdminTokenBundle:
        """Second factor (FR-2). Raises Invalid2faCodeError on a wrong
        code, ChallengeExpiredError if the Cognito session itself is no
        longer valid."""
        ...

    def admin_create_user(self, email: str, name: str) -> str:
        """AdminCreateUser, no password set (FR-3) — Pending status.
        Returns the new Cognito `sub`."""
        ...

    def admin_delete_user(self, email: str) -> None:
        """Saga compensation (FR-3) — deletes a just-created Cognito user
        when the paired Aurora insert fails."""
        ...

    def set_group(self, email: str, role: str) -> None:
        """Ensures Cognito group membership mirrors exactly one role —
        adds `role`'s group and removes every other fixed-role group."""
        ...

    def global_sign_out(self, email: str) -> None:
        """AdminUserGlobalSignOut (FR-4 deactivate) — revokes every
        refresh token for this admin immediately."""
        ...

    def revoke_refresh_token(self, refresh_token: str) -> None:
        """Revokes a single refresh token — used for role-change
        invalidation (FR-4) and max-concurrent-session LRU eviction
        (FR-5)."""
        ...


class AdminUserRepositoryPort(Protocol):
    def set_correlation_id(self, correlation_id: str) -> None: ...

    def get_by_email(self, email: str) -> AdminUser | None: ...

    def get_by_id(self, admin_id: str) -> AdminUser | None: ...

    def get_by_cognito_sub(self, cognito_sub: str) -> AdminUser | None:
        """Used by the FR-5 API Gateway authorizer to resolve the JWT's
        `sub` claim to a live status/role/IP-allowlist row on every
        request — never trusting the JWT's own (potentially stale)
        claims for authorization decisions."""
        ...

    def create(self, admin: AdminUser) -> AdminUser: ...

    def list(
        self,
        role: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> AdminUserPage: ...

    def update_role_and_config(
        self,
        admin_id: str,
        role: str | None,
        ip_allowlist: list[str] | None,
        max_concurrent_sessions: int | None,
        max_concurrent_sessions_set: bool,
    ) -> AdminUser: ...

    def set_status(self, admin_id: str, status: str) -> None: ...

    def set_last_login_now(self, admin_id: str) -> None: ...

    def count_active_super_admins(self) -> int: ...


class AdminChallengeStorePort(Protocol):
    def put(self, challenge: LoginChallenge, ttl_seconds: int) -> None: ...

    def get(self, challenge_token: str) -> LoginChallenge | None:
        """Returns None for an unknown OR naturally-expired token — the
        store itself enforces the TTL, callers don't need to separately
        check `expires_at`."""
        ...

    def consume(self, challenge_token: str) -> None:
        """Deletes the challenge — called only after a SUCCESSFUL 2FA
        verification (spec workflow: "single-use"). A wrong code does
        NOT consume it, allowing retries up to the lockout threshold."""
        ...


class AdminLockoutPort(Protocol):
    def record_failure(self, admin_id: str, window_seconds: int) -> int:
        """Increments the failure counter for `admin_id` within a
        rolling `window_seconds` window (15 minutes per spec FR-2) and
        returns the new count."""
        ...

    def is_locked(self, admin_id: str) -> bool: ...

    def lock(self, admin_id: str, ttl_seconds: int) -> None: ...

    def reset(self, admin_id: str) -> None:
        """Clears the failure counter on a successful verification."""
        ...


class AdminSessionRegistryPort(Protocol):
    def register_session(
        self, admin_id: str, refresh_token: str, max_concurrent_sessions: int | None
    ) -> list[str]:
        """Records a newly-issued refresh token for `admin_id`. If
        `max_concurrent_sessions` is set (0 is a valid, distinct value
        meaning "no sessions allowed" — not the same as `None`/
        unlimited) and this registration would exceed it, atomically
        evicts (and returns) however many of the oldest still-tracked
        refresh tokens are needed for LRU-eviction by the caller (FR-5)
        — the caller is responsible for actually revoking each one via
        Cognito. Returns an empty list when nothing was evicted."""
        ...

    def invalidate_all(self, admin_id: str) -> None:
        """Drops session bookkeeping for `admin_id` — called on
        deactivation/role-change since those already revoke via Cognito
        directly."""
        ...


class AdminEventPublisherPort(Protocol):
    def publish_admin_event(self, event_type: str, payload: dict, correlation_id: str) -> None:
        """Publishes one `admin.*` domain event using the standard
        envelope (services/README.md §5)."""
        ...
