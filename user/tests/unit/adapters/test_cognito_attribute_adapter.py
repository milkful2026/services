"""Cognito attribute adapter tests against moto's cognito-idp mock —
same fidelity caveats as MA-92's test_cognito_adapter.py: these validate
the adapter's boto3 call shape and control flow, not real Cognito
semantics."""

import boto3
import pytest
from botocore.exceptions import ClientError

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def pool_with_default_pincode_attribute(aws):
    """A pool schema that actually has custom:default_pincode defined —
    i.e. the state MA-92's stack needs to be updated to reach (see this
    adapter module's docstring)."""
    client = boto3.client("cognito-idp", region_name="ap-south-1")
    pool = client.create_user_pool(
        PoolName="milkful-test-pool",
        UsernameAttributes=["phone_number"],
        Schema=[
            {"Name": "phone_number", "AttributeDataType": "String", "Mutable": True},
            {"Name": "default_pincode", "AttributeDataType": "String", "Mutable": True},
        ],
    )
    pool_id = pool["UserPool"]["Id"]
    return {"client": client, "pool_id": pool_id}


def test_sync_profile_attributes_updates_existing_user(pool_with_default_pincode_attribute):
    client = pool_with_default_pincode_attribute["client"]
    pool_id = pool_with_default_pincode_attribute["pool_id"]
    client.admin_create_user(
        UserPoolId=pool_id,
        Username="+919876543210",
        UserAttributes=[{"Name": "phone_number", "Value": "+919876543210"}],
        MessageAction="SUPPRESS",
    )
    user_attrs = client.admin_get_user(UserPoolId=pool_id, Username="+919876543210")[
        "UserAttributes"
    ]
    cognito_sub = next(a["Value"] for a in user_attrs if a["Name"] == "sub")

    adapter = CognitoAttributeAdapter(user_pool_id=pool_id, region_name="ap-south-1")
    adapter.sync_profile_attributes(cognito_sub, "Priya Sharma", "560001")

    updated_attrs = client.admin_get_user(UserPoolId=pool_id, Username="+919876543210")[
        "UserAttributes"
    ]
    values = {a["Name"]: a["Value"] for a in updated_attrs}
    assert values["name"] == "Priya Sharma"
    assert values["custom:default_pincode"] == "560001"


_FAKE_UUID_SUB = "11111111-2222-3333-4444-555555555555"


def test_sync_profile_attributes_no_matching_user_is_a_noop(pool_with_default_pincode_attribute):
    adapter = CognitoAttributeAdapter(
        user_pool_id=pool_with_default_pincode_attribute["pool_id"], region_name="ap-south-1"
    )

    adapter.sync_profile_attributes(_FAKE_UUID_SUB, "Priya Sharma", "560001")  # must not raise


def test_sync_profile_attributes_wraps_list_users_client_error(
    pool_with_default_pincode_attribute, monkeypatch
):
    adapter = CognitoAttributeAdapter(
        user_pool_id=pool_with_default_pincode_attribute["pool_id"], region_name="ap-south-1"
    )

    def _raise(*args, **kwargs):
        raise ClientError({"Error": {"Code": "InternalErrorException"}}, "ListUsers")

    monkeypatch.setattr(adapter._client, "list_users", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.sync_profile_attributes(_FAKE_UUID_SUB, "Priya", "560001")


def test_sync_profile_attributes_rejects_non_uuid_sub_without_querying_cognito(
    pool_with_default_pincode_attribute, monkeypatch
):
    # A sub that could break out of the ListUsers Filter string (or is
    # otherwise not the UUID shape Cognito always assigns) must be
    # rejected outright, not interpolated into the filter.
    adapter = CognitoAttributeAdapter(
        user_pool_id=pool_with_default_pincode_attribute["pool_id"], region_name="ap-south-1"
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_users must not be called for a non-UUID sub")

    monkeypatch.setattr(adapter._client, "list_users", _fail_if_called)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.sync_profile_attributes('sub" OR "1"="1', "Priya", "560001")


def test_moto_does_not_enforce_schema_for_undefined_custom_attributes(cognito_user_pool):
    """Documents a moto fidelity gap discovered while writing this test:
    moto's admin_update_user_attributes does NOT reject writes to a
    custom attribute absent from the pool's schema, unlike real Cognito.
    This test intentionally asserts moto's (lenient) behavior rather than
    the real-AWS behavior it can't emulate — the cross-stack gap this
    module's docstring flags (MA-92's actual pool has no
    custom:default_pincode attribute) is real, but moto can't be used to
    demonstrate it failing; that needs a human against real/LocalStack
    Cognito.
    """
    client = cognito_user_pool["client"]
    pool_id = cognito_user_pool["pool_id"]
    client.admin_create_user(
        UserPoolId=pool_id,
        Username="+919876543210",
        UserAttributes=[{"Name": "phone_number", "Value": "+919876543210"}],
        MessageAction="SUPPRESS",
    )
    user_attrs = client.admin_get_user(UserPoolId=pool_id, Username="+919876543210")[
        "UserAttributes"
    ]
    cognito_sub = next(a["Value"] for a in user_attrs if a["Name"] == "sub")

    adapter = CognitoAttributeAdapter(user_pool_id=pool_id, region_name="ap-south-1")

    adapter.sync_profile_attributes(cognito_sub, "Priya Sharma", "560001")  # moto allows this
