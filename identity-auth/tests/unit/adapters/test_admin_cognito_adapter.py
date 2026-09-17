"""moto does not emulate the SOFTWARE_TOKEN_MFA challenge/response flow
at all (see admin_cognito_adapter.py's module docstring) — tests for
admin_password_auth / respond_to_mfa_challenge therefore monkeypatch the
underlying boto3 calls to simulate real Cognito's challenge shape, the
same technique already established for revoke_token in
test_cognito_adapter.py.
"""

import pytest
from botocore.exceptions import ClientError

from adapters.admin_cognito_adapter import AdminCognitoAdapter
from domain.admin_exceptions import (
    AdminEmailExistsError,
    ChallengeExpiredError,
    IncorrectAdminCredentialsError,
    Invalid2faCodeError,
)
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def adapter(admin_cognito_pool):
    return AdminCognitoAdapter(
        user_pool_id=admin_cognito_pool["pool_id"],
        client_id=admin_cognito_pool["app_client_id"],
        region_name="ap-south-1",
    )


def _create_active_admin(admin_cognito_pool, email="admin@milkful.test", password="Passw0rd!123"):
    client = admin_cognito_pool["client"]
    pool_id = admin_cognito_pool["pool_id"]
    client.admin_create_user(
        UserPoolId=pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    client.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)


def test_admin_create_user_returns_sub_and_leaves_user_pending(adapter, admin_cognito_pool):
    sub = adapter.admin_create_user("new-admin@milkful.test", "New Admin")

    assert sub
    user = admin_cognito_pool["client"].admin_get_user(
        UserPoolId=admin_cognito_pool["pool_id"], Username="new-admin@milkful.test"
    )
    assert user["UserStatus"] == "FORCE_CHANGE_PASSWORD"


def test_admin_create_user_duplicate_email_raises_conflict(adapter):
    adapter.admin_create_user("dup@milkful.test", "First")

    with pytest.raises(AdminEmailExistsError):
        adapter.admin_create_user("dup@milkful.test", "Second")


def test_admin_delete_user_is_idempotent(adapter):
    adapter.admin_create_user("temp@milkful.test", "Temp")

    adapter.admin_delete_user("temp@milkful.test")
    adapter.admin_delete_user("temp@milkful.test")  # second call must not raise


def test_set_group_adds_role_and_removes_others(adapter, admin_cognito_pool):
    email = "roled@milkful.test"
    adapter.admin_create_user(email, "Roled")

    adapter.set_group(email, "Ops")
    groups = admin_cognito_pool["client"].admin_list_groups_for_user(
        Username=email, UserPoolId=admin_cognito_pool["pool_id"]
    )
    assert {g["GroupName"] for g in groups["Groups"]} == {"Ops"}

    adapter.set_group(email, "Finance")
    groups = admin_cognito_pool["client"].admin_list_groups_for_user(
        Username=email, UserPoolId=admin_cognito_pool["pool_id"]
    )
    assert {g["GroupName"] for g in groups["Groups"]} == {"Finance"}


def test_global_sign_out_succeeds_for_existing_user(adapter, admin_cognito_pool):
    _create_active_admin(admin_cognito_pool)

    adapter.global_sign_out("admin@milkful.test")  # no exception == success


def test_admin_password_auth_wrong_password_raises_incorrect_credentials(adapter, admin_cognito_pool):
    _create_active_admin(admin_cognito_pool)

    with pytest.raises(IncorrectAdminCredentialsError):
        adapter.admin_password_auth("admin@milkful.test", "totally-wrong")


def test_admin_password_auth_unknown_user_raises_incorrect_credentials(adapter):
    with pytest.raises(IncorrectAdminCredentialsError):
        adapter.admin_password_auth("nobody@milkful.test", "whatever")


def test_admin_password_auth_fails_closed_when_pool_skips_mfa_challenge(adapter, admin_cognito_pool):
    # moto's real (unmocked-by-us) behavior: correct credentials succeed
    # immediately with no MFA challenge at all. The adapter must treat
    # this as a misconfiguration, not a valid login.
    _create_active_admin(admin_cognito_pool)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.admin_password_auth("admin@milkful.test", "Passw0rd!123")


def test_admin_password_auth_returns_session_when_mfa_challenge_present(adapter, monkeypatch):
    def _fake_initiate_auth(**kwargs):
        return {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "a" * 24}

    monkeypatch.setattr(adapter._client, "admin_initiate_auth", _fake_initiate_auth)

    session = adapter.admin_password_auth("admin@milkful.test", "Passw0rd!123")

    assert session == "a" * 24


def test_respond_to_mfa_challenge_success_returns_token_bundle(adapter, monkeypatch):
    def _fake_respond(**kwargs):
        return {
            "AuthenticationResult": {
                "AccessToken": "access-1",
                "RefreshToken": "refresh-1",
                "IdToken": "id-1",
                "ExpiresIn": 900,
            }
        }

    monkeypatch.setattr(adapter._client, "admin_respond_to_auth_challenge", _fake_respond)

    tokens = adapter.respond_to_mfa_challenge("admin@milkful.test", "session-1", "123456")

    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_in == 900


def test_respond_to_mfa_challenge_wrong_code_raises_invalid_2fa(adapter, monkeypatch):
    def _raise(**kwargs):
        raise adapter._client.exceptions.CodeMismatchException(
            {"Error": {"Code": "CodeMismatchException", "Message": "bad code"}}, "AdminRespondToAuthChallenge"
        )

    monkeypatch.setattr(adapter._client, "admin_respond_to_auth_challenge", _raise)

    with pytest.raises(Invalid2faCodeError):
        adapter.respond_to_mfa_challenge("admin@milkful.test", "session-1", "000000")


def test_respond_to_mfa_challenge_expired_session_raises_challenge_expired(adapter, monkeypatch):
    def _raise(**kwargs):
        raise adapter._client.exceptions.NotAuthorizedException(
            {"Error": {"Code": "NotAuthorizedException", "Message": "session expired"}},
            "AdminRespondToAuthChallenge",
        )

    monkeypatch.setattr(adapter._client, "admin_respond_to_auth_challenge", _raise)

    with pytest.raises(ChallengeExpiredError):
        adapter.respond_to_mfa_challenge("admin@milkful.test", "session-1", "123456")


def test_revoke_refresh_token_is_non_fatal_on_error(adapter, monkeypatch):
    def _raise(**kwargs):
        raise ClientError({"Error": {"Code": "SomeError"}}, "RevokeToken")

    monkeypatch.setattr(adapter._client, "revoke_token", _raise)

    adapter.revoke_refresh_token("some-refresh-token")  # must not raise
