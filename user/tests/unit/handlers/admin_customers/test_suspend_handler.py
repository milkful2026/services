import json
from datetime import date

import pytest

import handlers.admin_customers.suspend_handler as suspend_handler
from domain.exceptions import InvalidStatusTransitionError
from domain.models import CustomerAccount, CustomerStatus


class FakeCustomerStatusService:
    def __init__(self, result=None, raises=None):
        self.result = result or CustomerAccount(
            id="cust-1",
            name="Priya",
            mobile="+919876543210",
            email=None,
            account_type="B2C",
            status=CustomerStatus.SUSPENDED.value,
            status_reason="fraud",
            last_status_change_at=None,
        )
        self.raises = raises
        self.calls = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def suspend(self, customer_id, reason, until, actor_admin_id, now=None):
        self.calls.append((customer_id, reason, until, actor_admin_id))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def _reset_deps():
    suspend_handler._deps = None
    yield
    suspend_handler._deps = None


def _inject(result=None, raises=None):
    service = FakeCustomerStatusService(result=result, raises=raises)
    suspend_handler._deps = {"service": service}
    return service


def _event(body: dict, customer_id: str = "cust-1", admin_id: str = "admin-1") -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "pathParameters": {"id": customer_id},
        "requestContext": {
            "authorizer": {"lambda": {"adminId": admin_id, "email": "a@x.com", "role": "Ops"}}
        },
    }


def test_suspend_handler_success():
    service = _inject()
    response = suspend_handler.handler(
        _event({"reason": "fraud", "until": "2026-12-01"}), None
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["status"] == "Suspended"
    assert service.calls == [("cust-1", "fraud", date(2026, 12, 1), "admin-1")]


def test_suspend_handler_missing_authorizer_context_is_401():
    _inject()
    event = _event({"reason": "fraud", "until": "2026-12-01"})
    del event["requestContext"]["authorizer"]

    response = suspend_handler.handler(event, None)
    assert response["statusCode"] == 401


def test_suspend_handler_missing_path_id_is_error():
    _inject()
    event = _event({"reason": "fraud", "until": "2026-12-01"})
    event["pathParameters"] = None

    response = suspend_handler.handler(event, None)
    assert response["statusCode"] == 400


def test_suspend_handler_maps_invalid_status_transition_to_409():
    _inject(raises=InvalidStatusTransitionError("already deactivated"))
    response = suspend_handler.handler(_event({"reason": "fraud", "until": "2026-12-01"}), None)
    assert response["statusCode"] == 409
    body = json.loads(response["body"])
    assert body["data"]["errorCode"] == "INVALID_STATUS_TRANSITION"


def test_suspend_handler_missing_reason_is_validation_error():
    _inject()
    response = suspend_handler.handler(_event({"until": "2026-12-01"}), None)
    assert response["statusCode"] == 400


def test_suspend_handler_unexpected_exception_returns_500():
    _inject(raises=RuntimeError("boom"))
    response = suspend_handler.handler(_event({"reason": "fraud", "until": "2026-12-01"}), None)
    assert response["statusCode"] == 500
