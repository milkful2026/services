import json

import pytest

import handlers.admin_users.reactivate_handler as reactivate_handler
from domain.admin_exceptions import AdminNotFoundError
from domain.admin_models import AdminRole, AdminStatus, AdminUser


class FakeUserService:
    def __init__(self, updated=None, raise_error=None):
        self.updated = updated
        self.raise_error = raise_error

    def reactivate_admin(self, caller_role, target_id, correlation_id):
        if self.raise_error:
            raise self.raise_error
        return self.updated


@pytest.fixture(autouse=True)
def _reset_deps():
    reactivate_handler._deps = None
    yield
    reactivate_handler._deps = None


def _event(target_id="target-1"):
    return {
        "headers": {"x-request-id": "corr-1"},
        "pathParameters": {"id": target_id},
        "requestContext": {"authorizer": {"lambda": {"adminId": "caller-1", "email": "c@milkful.test", "role": "SuperAdmin"}}},
    }


def _admin() -> AdminUser:
    return AdminUser(
        id="target-1", cognito_sub="sub-1", name="A", email="a@milkful.test",
        role=AdminRole.OPS, status=AdminStatus.ACTIVE, ip_allowlist=[], max_concurrent_sessions=None, created_by=None,
    )


def test_reactivate_success_returns_active_admin():
    reactivate_handler._deps = {"user_service": FakeUserService(updated=_admin())}

    response = reactivate_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["status"] == "Active"


def test_reactivate_not_found_returns_404():
    reactivate_handler._deps = {"user_service": FakeUserService(raise_error=AdminNotFoundError())}

    response = reactivate_handler.handler(_event(target_id="missing"), None)

    assert response["statusCode"] == 404
