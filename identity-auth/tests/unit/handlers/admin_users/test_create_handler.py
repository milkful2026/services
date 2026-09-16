import json
import uuid

import pytest

import handlers.admin_users.create_handler as create_handler
from domain.admin_exceptions import AdminEmailExistsError, AdminForbiddenError
from domain.admin_models import AdminRole, AdminStatus, AdminUser


class FakeUserService:
    def __init__(self, created=None, raise_error=None):
        self.created = created
        self.raise_error = raise_error
        self.calls = []

    def create_admin(self, caller_role, caller_admin_id, name, email, role, correlation_id):
        self.calls.append((caller_role, caller_admin_id, name, email, role, correlation_id))
        if self.raise_error:
            raise self.raise_error
        return self.created


@pytest.fixture(autouse=True)
def _reset_deps():
    create_handler._deps = None
    yield
    create_handler._deps = None


def _authorized_event(body, caller_role="SuperAdmin", admin_id="caller-1"):
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"lambda": {"adminId": admin_id, "email": "caller@milkful.test", "role": caller_role}}},
    }


def _admin() -> AdminUser:
    return AdminUser(
        id=str(uuid.uuid4()), cognito_sub="sub-1", name="New Admin", email="new@milkful.test",
        role=AdminRole.OPS, status=AdminStatus.PENDING, ip_allowlist=[], max_concurrent_sessions=None, created_by="caller-1",
    )


def test_create_success_returns_201():
    admin = _admin()
    create_handler._deps = {"user_service": FakeUserService(created=admin)}

    response = create_handler.handler(
        _authorized_event({"name": "New Admin", "email": "new@milkful.test", "role": "Ops"}), None
    )

    assert response["statusCode"] == 201
    data = json.loads(response["body"])["data"]
    assert data["email"] == "new@milkful.test"
    assert data["status"] == "Pending"


def test_create_missing_authorizer_context_returns_401():
    create_handler._deps = {"user_service": FakeUserService()}

    response = create_handler.handler({"body": json.dumps({"name": "X", "email": "x@milkful.test", "role": "Ops"})}, None)

    assert response["statusCode"] == 401


def test_create_forbidden_for_non_super_admin_returns_403():
    create_handler._deps = {"user_service": FakeUserService(raise_error=AdminForbiddenError())}

    response = create_handler.handler(
        _authorized_event({"name": "X", "email": "x@milkful.test", "role": "Ops"}, caller_role="Ops"), None
    )

    assert response["statusCode"] == 403


def test_create_duplicate_email_returns_409():
    create_handler._deps = {"user_service": FakeUserService(raise_error=AdminEmailExistsError())}

    response = create_handler.handler(
        _authorized_event({"name": "X", "email": "dup@milkful.test", "role": "Ops"}), None
    )

    assert response["statusCode"] == 409


def test_create_missing_fields_returns_validation_error():
    create_handler._deps = {"user_service": FakeUserService()}

    response = create_handler.handler(_authorized_event({"email": "x@milkful.test"}), None)

    assert response["statusCode"] == 400
