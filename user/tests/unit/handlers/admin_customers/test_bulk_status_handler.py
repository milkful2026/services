import json

import pytest

import handlers.admin_customers.bulk_status_handler as bulk_status_handler
from domain.exceptions import ValidationError
from domain.models import BulkStatusResult


class FakeCustomerStatusService:
    def __init__(self, results=None, raises=None):
        self.results = results or [
            BulkStatusResult(customer_id="cust-1", success=True),
            BulkStatusResult(customer_id="cust-2", success=False, error_code="CUSTOMER_NOT_FOUND"),
        ]
        self.raises = raises
        self.calls = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def bulk_status_change(self, customer_ids, action, reason, until, actor_admin_id, now=None):
        self.calls.append((customer_ids, action, reason, until, actor_admin_id))
        if self.raises:
            raise self.raises
        return self.results


@pytest.fixture(autouse=True)
def _reset_deps():
    bulk_status_handler._deps = None
    yield
    bulk_status_handler._deps = None


def _inject(results=None, raises=None):
    service = FakeCustomerStatusService(results=results, raises=raises)
    bulk_status_handler._deps = {"service": service}
    return service


def _event(body: dict) -> dict:
    return {
        "body": json.dumps(body),
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {
            "authorizer": {"lambda": {"adminId": "admin-1", "email": "a@x.com", "role": "Ops"}}
        },
    }


def test_bulk_status_handler_returns_per_id_results_with_200_even_when_one_row_failed():
    _inject()
    response = bulk_status_handler.handler(
        _event({"customerIds": ["cust-1", "cust-2"], "action": "deactivate", "reason": "closure"}),
        None,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    results = body["data"]
    assert results[0] == {"customerId": "cust-1", "success": True, "errorCode": None}
    assert results[1] == {
        "customerId": "cust-2",
        "success": False,
        "errorCode": "CUSTOMER_NOT_FOUND",
    }


def test_bulk_status_handler_invalid_action_is_a_request_level_400():
    _inject(raises=ValidationError("action must be one of [...]"))
    response = bulk_status_handler.handler(
        _event({"customerIds": ["cust-1"], "action": "not-real"}), None
    )
    assert response["statusCode"] == 400
