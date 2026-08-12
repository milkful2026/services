"""Cognito adapter tests against moto's cognito-idp mock.

Known gap (flagged, not silently assumed): moto's cognito-idp mock has
limited fidelity — tokens returned by AdminInitiateAuth/InitiateAuth are
fake/unsigned-looking, and some admin APIs behave more leniently than real
Cognito (e.g. password policy enforcement). These tests validate the
adapter's boto3 call shape, control flow, and error mapping — not real
Cognito token semantics. Real token validation needs a human against a
real (or LocalStack) Cognito pool.
"""

import pytest
from botocore.exceptions import ClientError

from adapters.cognito_adapter import CognitoAdapter
from domain.exceptions import (
    ExternalServiceUnavailableError,
    InvalidRefreshTokenError,
    SocialAccountConflictError,
)


@pytest.fixture
def adapter(cognito_user_pool):
    return CognitoAdapter(
        user_pool_id=cognito_user_pool["pool_id"],
        client_id=cognito_user_pool["app_client_id"],
        region_name="ap-south-1",
    )


def test_find_verified_sub_by_phone_returns_none_when_no_user(adapter):
    assert adapter.find_verified_sub_by_phone("+919876543210") is None


def test_register_and_issue_tokens_creates_new_user(adapter):
    tokens, is_new_user = adapter.register_and_issue_tokens("+919876543210")

    assert is_new_user is True
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.id_token
    assert tokens.expires_in > 0


def test_find_verified_sub_by_phone_finds_user_after_registration(adapter):
    adapter.register_and_issue_tokens("+919876543210")

    sub = adapter.find_verified_sub_by_phone("+919876543210")

    assert sub is not None


def test_register_and_issue_tokens_second_call_confirms_not_creates(adapter):
    _, first_is_new = adapter.register_and_issue_tokens("+919876543210")
    _, second_is_new = adapter.register_and_issue_tokens("+919876543210")

    assert first_is_new is True
    assert second_is_new is False


def test_find_or_create_federated_user_creates_new(adapter):
    sub, mobile, is_new_user, mobile_verified = adapter.find_or_create_federated_user(
        "google", "google-sub-123", "user@example.com"
    )

    assert sub is not None
    assert mobile is None
    assert is_new_user is True
    assert mobile_verified is False


def test_find_or_create_federated_user_finds_existing_link(adapter):
    first_sub, _, _, _ = adapter.find_or_create_federated_user(
        "google", "google-sub-123", "user@example.com"
    )

    second_sub, _, is_new_user, _ = adapter.find_or_create_federated_user(
        "google", "google-sub-123", "user@example.com"
    )

    assert second_sub == first_sub
    assert is_new_user is False


def test_refresh_tokens_invalid_raises_domain_error(adapter):
    with pytest.raises(InvalidRefreshTokenError):
        adapter.refresh_tokens("not-a-real-refresh-token")


def test_find_or_create_federated_user_raises_conflict_for_existing_email(
    adapter, cognito_user_pool
):
    # Simulate a pre-existing phone-username record (from OTP registration)
    # that already owns this email attribute. Real Cognito's AdminGetUser
    # requires the literal Username (the phone number here) and would NOT
    # resolve by email — moto's mock is more lenient here (a documented
    # fidelity gap, see this module's docstring), so admin_get_user is
    # monkeypatched to match real-AWS semantics for this one call.
    cognito_user_pool["client"].admin_create_user(
        UserPoolId=cognito_user_pool["pool_id"],
        Username="+919876543210",
        UserAttributes=[
            {"Name": "phone_number", "Value": "+919876543210"},
            {"Name": "phone_number_verified", "Value": "true"},
            {"Name": "email", "Value": "user@example.com"},
        ],
        MessageAction="SUPPRESS",
    )

    real_admin_get_user = adapter._client.admin_get_user

    def _admin_get_user_by_literal_username_only(UserPoolId, Username):
        if Username == "user@example.com":
            raise adapter._client.exceptions.UserNotFoundException(
                {"Error": {"Code": "UserNotFoundException"}}, "AdminGetUser"
            )
        return real_admin_get_user(UserPoolId=UserPoolId, Username=Username)

    adapter._client.admin_get_user = _admin_get_user_by_literal_username_only

    with pytest.raises(SocialAccountConflictError) as exc_info:
        adapter.find_or_create_federated_user("google", "google-sub-123", "user@example.com")

    assert exc_info.value.details["mergeInstructionCode"] == "CONTACT_SUPPORT"


def test_register_and_issue_tokens_wraps_client_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise ClientError({"Error": {"Code": "InternalErrorException"}}, "AdminGetUser")

    monkeypatch.setattr(adapter._client, "admin_get_user", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.register_and_issue_tokens("+919876543210")
