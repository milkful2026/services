import pytest

from handlers.admin_context import AdminAuthenticationError, get_caller_admin


def test_get_caller_admin_reads_lambda_authorizer_context():
    event = {
        "requestContext": {
            "authorizer": {"lambda": {"adminId": "admin-1", "email": "a@x.com", "role": "Ops"}}
        }
    }
    caller = get_caller_admin(event)
    assert caller == {"adminId": "admin-1", "email": "a@x.com", "role": "Ops"}


def test_get_caller_admin_raises_when_context_missing():
    with pytest.raises(AdminAuthenticationError):
        get_caller_admin({})


def test_get_caller_admin_raises_when_role_missing():
    event = {"requestContext": {"authorizer": {"lambda": {"adminId": "admin-1"}}}}
    with pytest.raises(AdminAuthenticationError):
        get_caller_admin(event)
