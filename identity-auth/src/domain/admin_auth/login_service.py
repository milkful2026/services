"""Admin login orchestration (spec FR-1/FR-2/FR-5's session-eviction
half) — password step, TOTP 2FA step, lockout counting, and
max-concurrent-session LRU eviction.

No AWS SDK imports here (services/README.md §3.4) — only the abstract
adapter Protocols from adapters.admin_interfaces.
"""

import secrets
import time

from adapters.admin_interfaces import (
    AdminChallengeStorePort,
    AdminCognitoPort,
    AdminEventPublisherPort,
    AdminLockoutPort,
    AdminSessionRegistryPort,
    AdminUserRepositoryPort,
)
from domain.admin_exceptions import (
    AdminAccountDeactivatedError,
    AdminAccountLockedError,
    AdminAccountPendingError,
    ChallengeExpiredError,
    IncorrectAdminCredentialsError,
    Invalid2faCodeError,
)
from domain.admin_models import AdminStatus, AdminTokenBundle, LoginChallenge


class AdminLoginService:
    def __init__(
        self,
        admin_repo: AdminUserRepositoryPort,
        cognito: AdminCognitoPort,
        challenge_store: AdminChallengeStorePort,
        lockout: AdminLockoutPort,
        session_registry: AdminSessionRegistryPort,
        event_publisher: AdminEventPublisherPort,
        challenge_ttl_seconds: int = 300,
        lockout_max_attempts: int = 5,
        lockout_window_seconds: int = 900,
        lockout_duration_seconds: int = 900,
    ) -> None:
        self._admin_repo = admin_repo
        self._cognito = cognito
        self._challenge_store = challenge_store
        self._lockout = lockout
        self._session_registry = session_registry
        self._event_publisher = event_publisher
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._lockout_max_attempts = lockout_max_attempts
        self._lockout_window_seconds = lockout_window_seconds
        self._lockout_duration_seconds = lockout_duration_seconds

    def login_password(self, email: str, password: str, correlation_id: str) -> str:
        """FR-1. Returns a short-lived, single-use-on-success
        challengeToken. Never distinguishes "no such admin" from "wrong
        password" — both raise the same generic IncorrectAdminCredentialsError
        (no user enumeration)."""
        email = email.strip().lower()
        admin = self._admin_repo.get_by_email(email)
        if admin is None:
            raise IncorrectAdminCredentialsError()

        if admin.status == AdminStatus.PENDING:
            raise AdminAccountPendingError()
        if admin.status == AdminStatus.DEACTIVATED:
            raise AdminAccountDeactivatedError()

        try:
            cognito_session = self._cognito.admin_password_auth(email, password)
        except IncorrectAdminCredentialsError:
            # Only emitted once the email is confirmed to match a real
            # admin (spec FR-1) — the branch above already established
            # that; a lookup miss never reaches this point.
            self._event_publisher.publish_admin_event(
                "admin.login.failed",
                {"adminId": admin.id, "email": admin.email, "reason": "invalid_credentials"},
                correlation_id,
            )
            raise

        challenge_token = secrets.token_urlsafe(32)
        challenge = LoginChallenge(
            challenge_token=challenge_token,
            admin_id=admin.id,
            email=admin.email,
            cognito_session=cognito_session,
            expires_at=int(time.time()) + self._challenge_ttl_seconds,
        )
        self._challenge_store.put(challenge, self._challenge_ttl_seconds)
        return challenge_token

    def verify_2fa(self, challenge_token: str, code: str, correlation_id: str) -> AdminTokenBundle:
        """FR-2. Lockout is keyed by admin_user.id, not challengeToken —
        a new login always mints a fresh token, so a per-token counter
        would never accumulate across attempts (spec's own rationale)."""
        challenge = self._challenge_store.get(challenge_token)
        if challenge is None:
            raise ChallengeExpiredError()

        admin = self._admin_repo.get_by_id(challenge.admin_id)
        if admin is None:
            # Admin was deleted/desynced between password step and here —
            # treat the same as an expired challenge rather than leaking
            # a distinct error shape.
            raise ChallengeExpiredError()

        if self._lockout.is_locked(admin.id):
            raise AdminAccountLockedError()

        try:
            tokens = self._cognito.respond_to_mfa_challenge(challenge.email, challenge.cognito_session, code)
        except Invalid2faCodeError:
            count = self._lockout.record_failure(admin.id, self._lockout_window_seconds)
            self._event_publisher.publish_admin_event(
                "admin.login.failed",
                {"adminId": admin.id, "email": admin.email, "reason": "invalid_2fa_code"},
                correlation_id,
            )
            if count >= self._lockout_max_attempts:
                self._lockout.lock(admin.id, self._lockout_duration_seconds)
                self._event_publisher.publish_admin_event(
                    "admin.session.blocked",
                    {"adminId": admin.id, "email": admin.email, "reason": "lockout"},
                    correlation_id,
                )
                raise AdminAccountLockedError() from None
            raise

        # Success: consume the challenge, clear the failure counter, and
        # record the login before any best-effort side effects below.
        self._challenge_store.consume(challenge_token)
        self._lockout.reset(admin.id)
        self._admin_repo.set_last_login_now(admin.id)

        evicted_refresh_token = self._session_registry.register_session(
            admin.id, tokens.refresh_token, admin.max_concurrent_sessions
        )
        if evicted_refresh_token is not None:
            self._cognito.revoke_refresh_token(evicted_refresh_token)

        self._event_publisher.publish_admin_event(
            "admin.login.succeeded",
            {"adminId": admin.id, "email": admin.email, "role": admin.role.value},
            correlation_id,
        )
        return tokens
