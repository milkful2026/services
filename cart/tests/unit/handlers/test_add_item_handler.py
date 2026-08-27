import json

import pytest

import handlers.add_item_handler as add_item_handler
from domain.exceptions import OutOfStockError, ValidationError
from domain.models import Frequency, LineItem


class FakeCartService:
    def __init__(self, line_item=None, raises=None):
        self.line_item = line_item or LineItem(
            id="item-1", product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME,
            start_date=None, added_at="2026-08-28T00:00:00Z",
        )
        self.raises = raises
        self.calls: list[dict] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def add_item(self, user_id, product_id, quantity, frequency, start_date, idempotency_key):
        self.calls.append({
            "user_id": user_id, "product_id": product_id, "quantity": quantity,
            "frequency": frequency, "start_date": start_date, "idempotency_key": idempotency_key,
        })
        if self.raises:
            raise self.raises
        return self.line_item


@pytest.fixture(autouse=True)
def _reset_deps():
    add_item_handler._deps = None
    yield
    add_item_handler._deps = None


def _inject(line_item=None, raises=None):
    service = FakeCartService(line_item=line_item, raises=raises)
    add_item_handler._deps = {"cart_service": service}
    return service


def _event(body: dict, sub: str | None = "sub-123", headers: dict | None = None) -> dict:
    return {
        "headers": {"x-request-id": "corr-1", **(headers or {})},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub} if sub else {}}}},
        "body": json.dumps(body),
    }


def test_happy_path_returns_201_with_created_line_item():
    service = _inject()

    response = add_item_handler.handler(
        _event({"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}), None
    )

    assert response["statusCode"] == 201
    data = json.loads(response["body"])["data"]
    assert data["productId"] == "cow-milk"
    assert service.calls[0]["user_id"] == "sub-123"


def test_idempotency_key_header_is_forwarded():
    service = _inject()

    add_item_handler.handler(
        _event(
            {"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"},
            headers={"idempotency-key": "key-1"},
        ),
        None,
    )

    assert service.calls[0]["idempotency_key"] == "key-1"


def test_missing_jwt_claims_returns_400():
    _inject()

    response = add_item_handler.handler(
        _event({"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}, sub=None), None
    )

    assert response["statusCode"] == 400


def test_unrecognized_frequency_returns_400_validation_error():
    _inject()

    response = add_item_handler.handler(
        _event({"productId": "cow-milk", "quantity": 1, "frequency": "WEEKLY"}), None
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["data"]["errorCode"] == "VALIDATION_ERROR"


def test_malformed_json_body_returns_400():
    event = _event({"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"})
    event["body"] = "{not json"
    _inject()

    response = add_item_handler.handler(event, None)

    assert response["statusCode"] == 400


def test_out_of_stock_propagates_as_422():
    _inject(raises=OutOfStockError("not enough stock"))

    response = add_item_handler.handler(
        _event({"productId": "cow-milk", "quantity": 100, "frequency": "ONE_TIME"}), None
    )

    assert response["statusCode"] == 422
    assert json.loads(response["body"])["data"]["errorCode"] == "OUT_OF_STOCK"


def test_domain_validation_error_propagates_as_400():
    _inject(raises=ValidationError("startDate is required for a subscription item"))

    response = add_item_handler.handler(
        _event({"productId": "cow-milk", "quantity": 1, "frequency": "DAILY"}), None
    )

    assert response["statusCode"] == 400
