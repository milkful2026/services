import json
import uuid

import pytest

import handlers.admin_users.list_handler as list_handler
from domain.admin_exceptions import AdminForbiddenError
from domain.admin_models import AdminRole, AdminStatus, AdminUser, AdminUserPage


class FakeUserService:
    def __init__(self, page=None, raise_error=None):
        self.page = page
        self.raise_error = raise_error
        self.calls = []

    def list_admins(self, caller_role, role, status, search, page, page_size):
        self.calls.append((caller_role, role, status, search, page, page_size))
        if self.raise_error:
            raise self.raise_error
        return self.page


class FakeSettings:
    admin_default_page_size = 20


@pytest.fixture(autouse=True)
def _reset_deps():
    list_handler._deps = None
    yield
    list_handler._deps = None


def _event(query=None, caller_role="SuperAdmin"):
    return {
        "headers": {"x-request-id": "corr-1"},
        "queryStringParameters": query,
        "requestContext": {"authorizer": {"lambda": {"adminId": "caller-1", "email": "c@milkful.test", "role": caller_role}}},
    }


def _admin() -> AdminUser:
    return AdminUser(
        id=str(uuid.uuid4()), cognito_sub="sub-1", name="A", email="a@milkful.test",
        role=AdminRole.OPS, status=AdminStatus.ACTIVE, ip_allowlist=[], max_concurrent_sessions=None, created_by=None,
    )


def test_list_success_returns_page():
    page = AdminUserPage(items=[_admin()], total=1, page=1, page_size=20)
    list_handler._deps = {"user_service": FakeUserService(page=page), "settings": FakeSettings()}

    response = list_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["total"] == 1
    assert len(data["items"]) == 1


def test_list_passes_query_filters_through(deps_placeholder=None):
    fake_service = FakeUserService(page=AdminUserPage(items=[], total=0, page=2, page_size=10))
    list_handler._deps = {"user_service": fake_service, "settings": FakeSettings()}

    list_handler.handler(_event(query={"role": "Ops", "status": "Active", "search": "priya", "page": "2", "pageSize": "10"}), None)

    assert fake_service.calls[0] == ("SuperAdmin", "Ops", "Active", "priya", 2, 10)


def test_list_forbidden_for_non_super_admin():
    list_handler._deps = {"user_service": FakeUserService(raise_error=AdminForbiddenError()), "settings": FakeSettings()}

    response = list_handler.handler(_event(caller_role="Ops"), None)

    assert response["statusCode"] == 403


def test_list_missing_authorizer_context_returns_401():
    list_handler._deps = {"user_service": FakeUserService(), "settings": FakeSettings()}

    response = list_handler.handler({"headers": {}}, None)

    assert response["statusCode"] == 401
