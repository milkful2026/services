"""Domain-level tests for AdminLoginService, using simple in-memory fakes
for the adapter ports (not moto/fakeredis) — same style as
test_otp_service.py. The domain layer must not know or care what's
behind the Protocols."""

import uuid

import pytest

from domain.admin_exceptions import (
    AdminAccountDeactivatedError,
    AdminAccountLockedError,
    AdminAccountPendingError,
    ChallengeExpiredError,
    IncorrectAdminCredentialsError,
    Invalid2faCodeError,
)
from domain.admin_auth.login_service import AdminLoginService
from domain.admin_models import AdminRole, AdminStatus, AdminTokenBundle, AdminUser, LoginChallenge


class FakeAdminRepo:
    def __init__(self):
        self.by_id: dict[str, AdminUser] = {}
        self.last_login_calls: list[str] = []

    def add(self, admin: AdminUser) -> None:
        self.by_id[admin.id] = admin

    def get_by_email(self, email: str):
        for a in self.by_id.values():
            if a.email.lower() == email.lower():
                return a
        return None

    def get_by_id(self, admin_id: str):
        return self.by_id.get(admin_id)

    def set_last_login_now(self, admin_id: str) -> None:
        self.last_login_calls.append(admin_id)


class FakeCognito:
    def __init__(self):
        self.wrong_password_for: set[str] = set()
        self.wrong_code = False
        self.session_expired = False
        self.revoked_tokens: list[str] = []

    def admin_password_auth(self, email: str, password: str) -> str:
        if email in self.wrong_password_for:
            raise IncorrectAdminCredentialsError()
        return "cognito-session-1"

    def respond_to_mfa_challenge(self, email: str, session: str, code: str) -> AdminTokenBundle:
        if self.session_expired:
            raise ChallengeExpiredError()
        if self.wrong_code:
            raise Invalid2faCodeError()
        return AdminTokenBundle(access_token="access-1", refresh_token="refresh-1", id_token="id-1", expires_in=900)

    def revoke_refresh_token(self, refresh_token: str) -> None:
        self.revoked_tokens.append(refresh_token)


class FakeChallengeStore:
    def __init__(self):
        self.challenges: dict[str, LoginChallenge] = {}

    def put(self, challenge: LoginChallenge, ttl_seconds: int) -> None:
        self.challenges[challenge.challenge_token] = challenge

    def get(self, challenge_token: str):
        return self.challenges.get(challenge_token)

    def consume(self, challenge_token: str) -> None:
        self.challenges.pop(challenge_token, None)


class FakeLockout:
    def __init__(self):
        self.failures: dict[str, int] = {}
        self.locked: set[str] = set()

    def record_failure(self, admin_id: str, window_seconds: int) -> int:
        self.failures[admin_id] = self.failures.get(admin_id, 0) + 1
        return self.failures[admin_id]

    def is_locked(self, admin_id: str) -> bool:
        return admin_id in self.locked

    def lock(self, admin_id: str, ttl_seconds: int) -> None:
        self.locked.add(admin_id)

    def reset(self, admin_id: str) -> None:
        self.failures.pop(admin_id, None)
        self.locked.discard(admin_id)


class FakeSessionRegistry:
    def __init__(self, evict: str | None = None):
        self.evict = evict
        self.registered: list[tuple] = []

    def register_session(self, admin_id, refresh_token, max_concurrent_sessions):
        self.registered.append((admin_id, refresh_token, max_concurrent_sessions))
        return [self.evict] if self.evict is not None else []

    def invalidate_all(self, admin_id: str) -> None:
        pass


class FakeEventPublisher:
    def __init__(self):
        self.events: list[tuple] = []

    def publish_admin_event(self, event_type: str, payload: dict, correlation_id: str) -> None:
        self.events.append((event_type, payload, correlation_id))


def _admin(**overrides) -> AdminUser:
    defaults = dict(
        id=str(uuid.uuid4()),
        cognito_sub="sub-1",
        name="Priya",
        email="priya@milkful.test",
        role=AdminRole.OPS,
        status=AdminStatus.ACTIVE,
        ip_allowlist=[],
        max_concurrent_sessions=None,
        created_by=None,
    )
    defaults.update(overrides)
    return AdminUser(**defaults)


@pytest.fixture
def deps():
    return {
        "admin_repo": FakeAdminRepo(),
        "cognito": FakeCognito(),
        "challenge_store": FakeChallengeStore(),
        "lockout": FakeLockout(),
        "session_registry": FakeSessionRegistry(),
        "event_publisher": FakeEventPublisher(),
    }


@pytest.fixture
def service(deps):
    return AdminLoginService(**deps)


def test_login_password_unknown_email_raises_generic_401_no_event(service, deps):
    with pytest.raises(IncorrectAdminCredentialsError):
        service.login_password("nobody@milkful.test", "whatever", "corr-1")

    assert deps["event_publisher"].events == []


def test_login_password_pending_account_raises_pending_error(service, deps):
    deps["admin_repo"].add(_admin(status=AdminStatus.PENDING))

    with pytest.raises(AdminAccountPendingError):
        service.login_password("priya@milkful.test", "pw", "corr-1")


def test_login_password_deactivated_account_raises_deactivated_error(service, deps):
    deps["admin_repo"].add(_admin(status=AdminStatus.DEACTIVATED))

    with pytest.raises(AdminAccountDeactivatedError):
        service.login_password("priya@milkful.test", "pw", "corr-1")


def test_login_password_wrong_password_emits_failed_event(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    deps["cognito"].wrong_password_for.add(admin.email)

    with pytest.raises(IncorrectAdminCredentialsError):
        service.login_password(admin.email, "wrong", "corr-1")

    assert deps["event_publisher"].events == [
        ("admin.login.failed", {"adminId": admin.id, "email": admin.email, "reason": "invalid_credentials"}, "corr-1")
    ]


def test_login_password_success_returns_challenge_token(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)

    token = service.login_password(admin.email.upper(), "correct", "corr-1")

    assert token
    stored = deps["challenge_store"].challenges[token]
    assert stored.admin_id == admin.id


def test_verify_2fa_unknown_challenge_raises_expired(service):
    with pytest.raises(ChallengeExpiredError):
        service.verify_2fa("nonexistent-token", "123456", "corr-1")


def test_verify_2fa_locked_account_raises_locked(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    token = service.login_password(admin.email, "pw", "corr-1")
    deps["lockout"].locked.add(admin.id)

    with pytest.raises(AdminAccountLockedError):
        service.verify_2fa(token, "123456", "corr-1")


def test_verify_2fa_success_returns_tokens_and_emits_event(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    token = service.login_password(admin.email, "pw", "corr-1")

    tokens = service.verify_2fa(token, "123456", "corr-1")

    assert tokens.access_token == "access-1"
    assert admin.id in deps["admin_repo"].last_login_calls
    assert token not in deps["challenge_store"].challenges  # consumed
    assert (
        "admin.login.succeeded",
        {"adminId": admin.id, "email": admin.email, "role": "Ops"},
        "corr-1",
    ) in deps["event_publisher"].events


def test_verify_2fa_wrong_code_increments_failures_and_emits_event(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    deps["cognito"].wrong_code = True
    token = service.login_password(admin.email, "pw", "corr-1")

    with pytest.raises(Invalid2faCodeError):
        service.verify_2fa(token, "000000", "corr-1")

    assert deps["lockout"].failures[admin.id] == 1
    assert (
        "admin.login.failed",
        {"adminId": admin.id, "email": admin.email, "reason": "invalid_2fa_code"},
        "corr-1",
    ) in deps["event_publisher"].events


def test_verify_2fa_fifth_failure_locks_account_and_emits_blocked_event(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    deps["cognito"].wrong_code = True

    for i in range(4):
        token = service.login_password(admin.email, "pw", "corr-1")
        with pytest.raises(Invalid2faCodeError):
            service.verify_2fa(token, "000000", "corr-1")

    token = service.login_password(admin.email, "pw", "corr-1")
    with pytest.raises(AdminAccountLockedError):
        service.verify_2fa(token, "000000", "corr-1")

    assert admin.id in deps["lockout"].locked
    assert (
        "admin.session.blocked",
        {"adminId": admin.id, "email": admin.email, "reason": "lockout"},
        "corr-1",
    ) in deps["event_publisher"].events


def test_verify_2fa_expired_session_raises_challenge_expired(service, deps):
    admin = _admin()
    deps["admin_repo"].add(admin)
    deps["cognito"].session_expired = True
    token = service.login_password(admin.email, "pw", "corr-1")

    with pytest.raises(ChallengeExpiredError):
        service.verify_2fa(token, "123456", "corr-1")


def test_verify_2fa_evicts_oldest_session_when_over_limit(deps):
    deps["session_registry"] = FakeSessionRegistry(evict="old-refresh-token")
    service = AdminLoginService(**deps)
    admin = _admin(max_concurrent_sessions=1)
    deps["admin_repo"].add(admin)
    token = service.login_password(admin.email, "pw", "corr-1")

    service.verify_2fa(token, "123456", "corr-1")

    assert "old-refresh-token" in deps["cognito"].revoked_tokens
