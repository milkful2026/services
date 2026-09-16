import json

import pytest

import handlers.admin_users.deactivate_handler as deactivate_handler
from domain.admin_exceptions import SelfDeactivationError
from domain.admin_models import AdminRole, AdminStatus, AdminUser


class FakeUserService:
    def __init__(self, updated=None, raise_error=None):
        self.updated = updated
        self.raise_error = raise_error
        self.calls = []

    def deactivate_admin(self, caller_role, caller_admin_id, target_id, correlation_id):
        self.calls.append((caller_role, caller_admin_id, target_id, correlation_id))
        if self.raise_error:
            raise self.raise_error
        return self.updated


@pytest.fixture(autouse=True)
def _reset_deps():
    deactivate_handler._deps = None
    yield
    deactivate_handler._deps = None


def _event(target_id="target-1", caller_id="caller-1"):
    return {
        "headers": {"x-request-id": "corr-1"},
        "pathParameters": {"id": target_id},
        "requestContext": {"authorizer": {"lambda": {"adminId": caller_id, "email": "c@milkful.test", "role": "SuperAdmin"}}},
    }


def _admin() -> AdminUser:
    return AdminUser(
        id="target-1", cognito_sub="sub-1", name="A", email="a@milkful.test",
        role=AdminRole.OPS, status=AdminStatus.DEACTIVATED, ip_allowlist=[], max_concurrent_sessions=None, created_by=None,
    )


def test_deactivate_success_returns_updated_admin():
    deactivate_handler._deps = {"user_service": FakeUserService(updated=_admin())}

    response = deactivate_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["status"] == "Deactivated"


def test_deactivate_self_returns_400():
    deactivate_handler._deps = {"user_service": FakeUserService(raise_error=SelfDeactivationError())}

    response = deactivate_handler.handler(_event(target_id="caller-1", caller_id="caller-1"), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["data"]["errorCode"] == "SELF_DEACTIVATION_NOT_ALLOWED"
