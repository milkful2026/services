"""Handler -> domain -> datastore integration tests for the MA-129 admin
identity/RBAC journey, moto+fakeredis-backed end to end — same pattern as
test_login_flow.py.

**Spec gap, not silently guessed around**: the spec (FR-3/FR-4) defines
how an admin is created (Pending) and reactivated (Deactivated ->
Active), but no endpoint transitions Pending -> Active after a newly-
invited admin completes their password-set + TOTP enrollment — FR-1 is
explicit that a Pending account cannot log in at all. This is flagged in
this service's README as an unresolved spec question. The E2E test below
simulates that out-of-scope "admin completed their invitation" step by
updating Aurora directly, so the parts of the journey this spec DOES
fully specify (login, 2FA, lockout, deactivate, blocked re-login) are
still exercised faithfully end to end.

**Known test-fidelity gap**: moto does not implement the
SOFTWARE_TOKEN_MFA challenge/response flow at all (see
admin_cognito_adapter.py's module docstring) — `AdminCognitoAdapter.
admin_password_auth` / `.respond_to_mfa_challenge` are monkeypatched at
the class level (affecting every instance built by every handler module
in this test) to simulate real Cognito's two-step behavior, with a fixed
test TOTP code. Cognito user/group state itself (AdminCreateUser,
AdminAddUserToGroup, AdminUserGlobalSignOut) IS exercised for real
through moto.
"""

import json

import pytest
from freezegun import freeze_time

import handlers.admin_auth.login_handler as login_handler
import handlers.admin_auth.verify_2fa_handler as verify_2fa_handler
import handlers.admin_users.composition as admin_users_composition
import handlers.admin_users.create_handler as create_handler
import handlers.admin_users.deactivate_handler as deactivate_handler
import handlers.admin_users.list_handler as list_handler
import handlers.admin_users.reactivate_handler as reactivate_handler
import handlers.admin_users.update_handler as update_handler
from adapters.admin_cognito_adapter import AdminCognitoAdapter
from adapters.admin_user_repository import SqlAlchemyAdminUserRepository, create_schema
from domain.admin_exceptions import IncorrectAdminCredentialsError, Invalid2faCodeError
from domain.admin_models import AdminRole, AdminStatus, AdminTokenBundle, AdminUser

_FIXED_TOTP_CODE = "111111"


@pytest.fixture(autouse=True)
def _reset_handler_deps():
    modules = [
        login_handler,
        verify_2fa_handler,
        create_handler,
        list_handler,
        update_handler,
        deactivate_handler,
        reactivate_handler,
    ]
    for m in modules:
        m._deps = None
    yield
    for m in modules:
        m._deps = None


@pytest.fixture
def admin_engine(tmp_path):
    from sqlalchemy import create_engine

    # A file-backed SQLite DB (not :memory:) so every handler module's
    # own, independently-constructed SQLAlchemy engine sees the same
    # data — an in-memory DB is isolated per connection/engine object.
    db_path = tmp_path / "admin_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def wired_env(admin_cognito_pool, admin_engine, event_bus, fake_redis, monkeypatch):
    monkeypatch.setenv("IDENTITY_AUTH_ADMIN_DATABASE_URL", str(admin_engine.url))

    for mod in (login_handler, verify_2fa_handler):
        monkeypatch.setattr(mod, "build_redis_client", lambda *a, **kw: fake_redis)
    monkeypatch.setattr(admin_users_composition, "build_redis_client", lambda *a, **kw: fake_redis)

    captured_events: list[tuple] = []

    def _capture(self, event_type, payload, correlation_id):
        captured_events.append((event_type, payload, correlation_id))

    monkeypatch.setattr(
        "adapters.notification_publisher.EventBridgeNotificationPublisher.publish_admin_event", _capture
    )

    known_passwords: dict[str, str] = {}

    def _fake_password_auth(self, email, password):
        if known_passwords.get(email) != password:
            raise IncorrectAdminCredentialsError()
        return f"session-for-{email}"

    def _fake_respond(self, email, session, code):
        if code != _FIXED_TOTP_CODE:
            raise Invalid2faCodeError()
        return AdminTokenBundle(
            access_token=f"access-{email}", refresh_token=f"refresh-{email}", id_token=f"id-{email}", expires_in=900
        )

    monkeypatch.setattr(AdminCognitoAdapter, "admin_password_auth", _fake_password_auth)
    monkeypatch.setattr(AdminCognitoAdapter, "respond_to_mfa_challenge", _fake_respond)

    return {"known_passwords": known_passwords, "events": captured_events}


def _seed_super_admin(admin_engine, admin_cognito_pool, email="superadmin@milkful.test") -> AdminUser:
    """Stands in for scripts/bootstrap_super_admin.py's one-time, human-run
    seeding — see this service's README for why that's not a self-service
    API call (no Super-Admin exists yet to authorize creating one)."""
    client = admin_cognito_pool["client"]
    pool_id = admin_cognito_pool["pool_id"]
    client.admin_create_user(
        UserPoolId=pool_id,
        Username=email,
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
        MessageAction="SUPPRESS",
    )
    client.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName="SuperAdmin")
    user = client.admin_get_user(UserPoolId=pool_id, Username=email)
    sub = {a["Name"]: a["Value"] for a in user["UserAttributes"]}["sub"]

    repo = SqlAlchemyAdminUserRepository(admin_engine)
    return repo.create(
        AdminUser(
            id="bootstrap-super-admin",
            cognito_sub=sub,
            name="Bootstrap Super Admin",
            email=email,
            role=AdminRole.SUPER_ADMIN,
            status=AdminStatus.PENDING,
            ip_allowlist=[],
            max_concurrent_sessions=None,
            created_by=None,
        )
    )


def _activate(admin_engine, admin_id: str) -> None:
    repo = SqlAlchemyAdminUserRepository(admin_engine)
    repo.set_status(admin_id, "Active")


def _login_and_verify(email: str, password: str, code: str = _FIXED_TOTP_CODE) -> dict:
    login_resp = login_handler.handler(
        {"body": json.dumps({"email": email, "password": password}), "headers": {"x-request-id": "corr-login"}}, None
    )
    assert login_resp["statusCode"] == 200, login_resp["body"]
    challenge_token = json.loads(login_resp["body"])["data"]["challengeToken"]

    verify_resp = verify_2fa_handler.handler(
        {
            "body": json.dumps({"challengeToken": challenge_token, "code": code}),
            "headers": {"x-request-id": "corr-2fa"},
        },
        None,
    )
    return verify_resp


def _authorized_event(body_dict, caller, target_id=None, query=None, method_body=True):
    event = {
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {
            "authorizer": {"lambda": {"adminId": caller["id"], "email": caller["email"], "role": caller["role"]}}
        },
    }
    if method_body:
        event["body"] = json.dumps(body_dict or {})
    if target_id is not None:
        event["pathParameters"] = {"id": target_id}
    if query is not None:
        event["queryStringParameters"] = query
    return event


def test_full_admin_journey(wired_env, admin_engine, admin_cognito_pool):
    known_passwords = wired_env["known_passwords"]
    events = wired_env["events"]

    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    known_passwords[super_admin.email] = "SuperSecret123!"

    # 1. Super-Admin logs in.
    verify_resp = _login_and_verify(super_admin.email, "SuperSecret123!")
    assert verify_resp["statusCode"] == 200
    super_admin_tokens = json.loads(verify_resp["body"])["data"]
    assert super_admin_tokens["accessToken"]
    assert any(evt[0] == "admin.login.succeeded" for evt in events)

    caller = {"id": super_admin.id, "email": super_admin.email, "role": "SuperAdmin"}

    # 2. Super-Admin creates a new Ops admin.
    create_resp = create_handler.handler(
        _authorized_event({"name": "New Ops Admin", "email": "newops@milkful.test", "role": "Ops"}, caller), None
    )
    assert create_resp["statusCode"] == 201, create_resp["body"]
    new_admin = json.loads(create_resp["body"])["data"]
    assert new_admin["status"] == "Pending"
    assert any(evt[0] == "admin.user.created" for evt in events)

    # Spec gap noted in the module docstring: nothing in this spec
    # transitions Pending -> Active. Simulated here as an out-of-band
    # "admin completed their invitation" step.
    _activate(admin_engine, new_admin["id"])
    known_passwords["newops@milkful.test"] = "NewOpsPass1!"

    # 3. The new admin logs in with 2FA.
    new_admin_verify = _login_and_verify("newops@milkful.test", "NewOpsPass1!")
    assert new_admin_verify["statusCode"] == 200

    # 4. Super-Admin lists admins and finds the new one.
    list_resp = list_handler.handler(_authorized_event(None, caller, query=None, method_body=False), None)
    assert list_resp["statusCode"] == 200
    listed_emails = {item["email"] for item in json.loads(list_resp["body"])["data"]["items"]}
    assert "newops@milkful.test" in listed_emails

    # 5. Super-Admin deactivates the new admin.
    deactivate_resp = deactivate_handler.handler(
        _authorized_event(None, caller, target_id=new_admin["id"]), None
    )
    assert deactivate_resp["statusCode"] == 200
    assert json.loads(deactivate_resp["body"])["data"]["status"] == "Deactivated"
    assert any(evt[0] == "admin.user.deactivated" for evt in events)

    # 6. The deactivated admin can no longer log in.
    blocked_login_resp = login_handler.handler(
        {
            "body": json.dumps({"email": "newops@milkful.test", "password": "NewOpsPass1!"}),
            "headers": {"x-request-id": "corr-blocked"},
        },
        None,
    )
    assert blocked_login_resp["statusCode"] == 403
    assert json.loads(blocked_login_resp["body"])["data"]["errorCode"] == "ADMIN_ACCOUNT_DEACTIVATED"


def test_login_wrong_password_returns_generic_401(wired_env, admin_engine, admin_cognito_pool):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    wired_env["known_passwords"][super_admin.email] = "SuperSecret123!"

    response = login_handler.handler(
        {
            "body": json.dumps({"email": super_admin.email, "password": "wrong-password"}),
            "headers": {"x-request-id": "corr-1"},
        },
        None,
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "INCORRECT_CREDENTIALS"


def test_login_unknown_email_returns_same_generic_401(wired_env):
    response = login_handler.handler(
        {
            "body": json.dumps({"email": "nobody@milkful.test", "password": "whatever"}),
            "headers": {"x-request-id": "corr-1"},
        },
        None,
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "INCORRECT_CREDENTIALS"


def test_2fa_lockout_after_five_wrong_codes(wired_env, admin_engine, admin_cognito_pool):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    wired_env["known_passwords"][super_admin.email] = "SuperSecret123!"

    def _login_then_wrong_code():
        login_resp = login_handler.handler(
            {
                "body": json.dumps({"email": super_admin.email, "password": "SuperSecret123!"}),
                "headers": {"x-request-id": "corr-1"},
            },
            None,
        )
        token = json.loads(login_resp["body"])["data"]["challengeToken"]
        return verify_2fa_handler.handler(
            {"body": json.dumps({"challengeToken": token, "code": "000000"}), "headers": {}}, None
        )

    for _ in range(4):
        resp = _login_then_wrong_code()
        assert resp["statusCode"] == 401
        assert json.loads(resp["body"])["data"]["errorCode"] == "INVALID_2FA_CODE"

    fifth = _login_then_wrong_code()
    assert fifth["statusCode"] == 401
    assert json.loads(fifth["body"])["data"]["errorCode"] == "ADMIN_ACCOUNT_LOCKED"
    assert any(evt[0] == "admin.session.blocked" for evt in wired_env["events"])

    # Even a correct login now hits the lock before Cognito is even asked.
    sixth_login = login_handler.handler(
        {
            "body": json.dumps({"email": super_admin.email, "password": "SuperSecret123!"}),
            "headers": {"x-request-id": "corr-1"},
        },
        None,
    )
    challenge_token = json.loads(sixth_login["body"])["data"]["challengeToken"]
    still_locked = verify_2fa_handler.handler(
        {"body": json.dumps({"challengeToken": challenge_token, "code": _FIXED_TOTP_CODE}), "headers": {}}, None
    )
    assert still_locked["statusCode"] == 401
    assert json.loads(still_locked["body"])["data"]["errorCode"] == "ADMIN_ACCOUNT_LOCKED"


def test_2fa_verify_with_expired_challenge_token_returns_distinct_code(wired_env):
    response = verify_2fa_handler.handler(
        {"body": json.dumps({"challengeToken": "not-a-real-token", "code": "123456"}), "headers": {}}, None
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["data"]["errorCode"] == "CHALLENGE_EXPIRED"


def test_create_admin_forbidden_for_non_super_admin(wired_env, admin_engine, admin_cognito_pool):
    caller = {"id": "some-ops-admin", "email": "ops@milkful.test", "role": "Ops"}

    response = create_handler.handler(
        _authorized_event({"name": "X", "email": "x@milkful.test", "role": "Ops"}, caller), None
    )

    assert response["statusCode"] == 403


def test_create_admin_duplicate_email_returns_409(wired_env, admin_engine, admin_cognito_pool):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    caller = {"id": super_admin.id, "email": super_admin.email, "role": "SuperAdmin"}

    first = create_handler.handler(
        _authorized_event({"name": "First", "email": "dup@milkful.test", "role": "Ops"}, caller), None
    )
    assert first["statusCode"] == 201

    second = create_handler.handler(
        _authorized_event({"name": "Second", "email": "dup@milkful.test", "role": "Finance"}, caller), None
    )
    assert second["statusCode"] == 409


def test_self_deactivation_returns_400(wired_env, admin_engine, admin_cognito_pool):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    caller = {"id": super_admin.id, "email": super_admin.email, "role": "SuperAdmin"}

    response = deactivate_handler.handler(_authorized_event(None, caller, target_id=super_admin.id), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["data"]["errorCode"] == "SELF_DEACTIVATION_NOT_ALLOWED"


def test_update_admin_role_change_forces_relogin_via_global_sign_out(
    wired_env, admin_engine, admin_cognito_pool
):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    caller = {"id": super_admin.id, "email": super_admin.email, "role": "SuperAdmin"}

    create_resp = create_handler.handler(
        _authorized_event({"name": "Role Change", "email": "rolechange@milkful.test", "role": "Ops"}, caller), None
    )
    target_id = json.loads(create_resp["body"])["data"]["id"]

    update_resp = update_handler.handler(
        _authorized_event({"role": "Finance"}, caller, target_id=target_id), None
    )

    assert update_resp["statusCode"] == 200
    assert json.loads(update_resp["body"])["data"]["role"] == "Finance"
    assert any(evt[0] == "admin.role.assigned" for evt in wired_env["events"])


def test_reactivate_after_deactivate_requires_new_login(wired_env, admin_engine, admin_cognito_pool):
    super_admin = _seed_super_admin(admin_engine, admin_cognito_pool)
    _activate(admin_engine, super_admin.id)
    caller = {"id": super_admin.id, "email": super_admin.email, "role": "SuperAdmin"}

    create_resp = create_handler.handler(
        _authorized_event({"name": "Reactivate Me", "email": "reactivate@milkful.test", "role": "Ops"}, caller), None
    )
    target_id = json.loads(create_resp["body"])["data"]["id"]
    _activate(admin_engine, target_id)

    deactivate_handler.handler(_authorized_event(None, caller, target_id=target_id), None)
    reactivate_resp = reactivate_handler.handler(_authorized_event(None, caller, target_id=target_id), None)

    assert reactivate_resp["statusCode"] == 200
    assert json.loads(reactivate_resp["body"])["data"]["status"] == "Active"
    assert any(evt[0] == "admin.user.reactivated" for evt in wired_env["events"])
