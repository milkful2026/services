import uuid

import pytest

import handlers.admin_authorizer_handler as authorizer_handler
from domain.admin_models import AdminRole, AdminStatus, AdminUser


class FakeVerifier:
    def __init__(self, claims=None, raise_error=None):
        self.claims = claims or {"sub": "sub-1"}
        self.raise_error = raise_error

    def verify_access_token(self, token):
        if self.raise_error:
            raise self.raise_error
        return self.claims


class FakeRepo:
    def __init__(self, admin=None):
        self.admin = admin

    def get_by_cognito_sub(self, sub):
        return self.admin


class FakePublisher:
    def __init__(self):
        self.events = []

    def publish_admin_event(self, event_type, payload, correlation_id):
        self.events.append((event_type, payload, correlation_id))


@pytest.fixture(autouse=True)
def _reset_deps():
    authorizer_handler._deps = None
    yield
    authorizer_handler._deps = None


def _admin(**overrides) -> AdminUser:
    defaults = dict(
        id=str(uuid.uuid4()),
        cognito_sub="sub-1",
        name="Admin",
        email="admin@milkful.test",
        role=AdminRole.OPS,
        status=AdminStatus.ACTIVE,
        ip_allowlist=[],
        max_concurrent_sessions=None,
        created_by=None,
    )
    defaults.update(overrides)
    return AdminUser(**defaults)


def _wire(verifier=None, repo=None, publisher=None):
    authorizer_handler._deps = {
        "verifier": verifier or FakeVerifier(),
        "repo": repo or FakeRepo(),
        "publisher": publisher or FakePublisher(),
    }


def _event(token: str | None = "valid-token", source_ip: str = "10.0.0.5"):
    headers = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {"headers": headers, "requestContext": {"http": {"sourceIp": source_ip}}}


def test_missing_authorization_header_denies():
    _wire()

    result = authorizer_handler.handler(_event(token=None), None)

    assert result["isAuthorized"] is False


def test_invalid_token_denies():
    from domain.admin_exceptions import AdminAuthenticationError

    _wire(verifier=FakeVerifier(raise_error=AdminAuthenticationError()))

    result = authorizer_handler.handler(_event(), None)

    assert result["isAuthorized"] is False


def test_unknown_admin_denies():
    _wire(repo=FakeRepo(admin=None))

    result = authorizer_handler.handler(_event(), None)

    assert result["isAuthorized"] is False


def test_inactive_admin_denies():
    _wire(repo=FakeRepo(admin=_admin(status=AdminStatus.DEACTIVATED)))

    result = authorizer_handler.handler(_event(), None)

    assert result["isAuthorized"] is False


def test_active_admin_within_allowed_ip_authorized():
    admin = _admin(ip_allowlist=["10.0.0.0/24"])
    _wire(repo=FakeRepo(admin=admin))

    result = authorizer_handler.handler(_event(source_ip="10.0.0.5"), None)

    assert result["isAuthorized"] is True
    assert result["context"]["adminId"] == admin.id
    assert result["context"]["role"] == "Ops"


def test_active_admin_outside_allowed_ip_denies_and_emits_event():
    admin = _admin(ip_allowlist=["10.0.0.0/24"])
    publisher = FakePublisher()
    _wire(repo=FakeRepo(admin=admin), publisher=publisher)

    result = authorizer_handler.handler(_event(source_ip="203.0.113.9"), None)

    assert result["isAuthorized"] is False
    assert publisher.events[0][0] == "admin.session.blocked"
    assert publisher.events[0][1]["reason"] == "ip_not_allowed"


def test_active_admin_with_empty_allowlist_authorized_from_any_ip():
    admin = _admin(ip_allowlist=[])
    _wire(repo=FakeRepo(admin=admin))

    result = authorizer_handler.handler(_event(source_ip="203.0.113.9"), None)

    assert result["isAuthorized"] is True
