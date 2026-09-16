import json
import uuid

import pytest

import handlers.admin_users.update_handler as update_handler
from domain.admin_exceptions import AdminNotFoundError, InvalidCidrError
from domain.admin_models import AdminRole, AdminStatus, AdminUser


class FakeUserService:
    def __init__(self, updated=None, raise_error=None):
        self.updated = updated
        self.raise_error = raise_error
        self.calls = []

    def update_admin(self, caller_role, target_id, role, ip_allowlist, ip_allowlist_set, max_concurrent_sessions, max_concurrent_sessions_set, correlation_id):
        self.calls.append(
            dict(
                caller_role=caller_role, target_id=target_id, role=role, ip_allowlist=ip_allowlist,
                ip_allowlist_set=ip_allowlist_set, max_concurrent_sessions=max_concurrent_sessions,
                max_concurrent_sessions_set=max_concurrent_sessions_set,
            )
        )
        if self.raise_error:
            raise self.raise_error
        return self.updated


@pytest.fixture(autouse=True)
def _reset_deps():
    update_handler._deps = None
    yield
    update_handler._deps = None


def _event(body, target_id="target-1", caller_role="SuperAdmin"):
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "pathParameters": {"id": target_id},
        "requestContext": {"authorizer": {"lambda": {"adminId": "caller-1", "email": "c@milkful.test", "role": caller_role}}},
    }


def _admin() -> AdminUser:
    return AdminUser(
        id="target-1", cognito_sub="sub-1", name="A", email="a@milkful.test",
        role=AdminRole.FINANCE, status=AdminStatus.ACTIVE, ip_allowlist=["10.0.0.0/24"],
        max_concurrent_sessions=3, created_by=None,
    )


def test_update_role_only_leaves_ip_allowlist_flag_unset():
    fake_service = FakeUserService(updated=_admin())
    update_handler._deps = {"user_service": fake_service}

    response = update_handler.handler(_event({"role": "Finance"}), None)

    assert response["statusCode"] == 200
    call = fake_service.calls[0]
    assert call["role"] == "Finance"
    assert call["ip_allowlist_set"] is False
    assert call["max_concurrent_sessions_set"] is False


def test_update_ip_allowlist_explicit_empty_list_sets_flag_true():
    fake_service = FakeUserService(updated=_admin())
    update_handler._deps = {"user_service": fake_service}

    update_handler.handler(_event({"ipAllowlist": []}), None)

    call = fake_service.calls[0]
    assert call["ip_allowlist_set"] is True
    assert call["ip_allowlist"] == []


def test_update_max_concurrent_sessions_explicit_null_sets_flag_true():
    fake_service = FakeUserService(updated=_admin())
    update_handler._deps = {"user_service": fake_service}

    update_handler.handler(_event({"maxConcurrentSessions": None}), None)

    call = fake_service.calls[0]
    assert call["max_concurrent_sessions_set"] is True
    assert call["max_concurrent_sessions"] is None


def test_update_missing_path_id_returns_error():
    update_handler._deps = {"user_service": FakeUserService()}

    event = _event({"role": "Ops"})
    event["pathParameters"] = {}

    response = update_handler.handler(event, None)

    assert response["statusCode"] == 400


def test_update_invalid_cidr_returns_422():
    update_handler._deps = {"user_service": FakeUserService(raise_error=InvalidCidrError("Invalid CIDR range: 'not-a-cidr'"))}

    response = update_handler.handler(_event({"ipAllowlist": ["not-a-cidr"]}), None)

    assert response["statusCode"] == 422


def test_update_not_found_returns_404():
    update_handler._deps = {"user_service": FakeUserService(raise_error=AdminNotFoundError())}

    response = update_handler.handler(_event({"role": "Ops"}, target_id="missing"), None)

    assert response["statusCode"] == 404
