"""MA-135 FR-3/FR-4 — Order Service's internal cart read/clear handlers.
IAM auth is enforced by API Gateway (see the infra test), so these only
cover path-parameter handling, body validation and error mapping."""

import json

import pytest

import handlers.internal_get_cart_handler as internal_get_cart_handler
import handlers.internal_remove_items_handler as internal_remove_items_handler
from domain.exceptions import CartVersionMismatchError
from domain.models import Cart, Frequency, LineItem


class FakeCartService:
    def __init__(self, cart=None, raises=None):
        self.cart = cart or Cart()
        self.raises = raises
        self.calls: list[tuple] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_cart_internal(self, user_id):
        self.calls.append(("get", user_id))
        if self.raises:
            raise self.raises
        return self.cart

    def remove_items_internal(self, user_id, item_ids, if_version, checkout_id):
        self.calls.append(("remove", user_id, item_ids, if_version, checkout_id))
        if self.raises:
            raise self.raises
        return self.cart


@pytest.fixture(autouse=True)
def _reset_deps():
    internal_get_cart_handler._deps = None
    internal_remove_items_handler._deps = None
    yield
    internal_get_cart_handler._deps = None
    internal_remove_items_handler._deps = None


def _inject(module, cart=None, raises=None):
    service = FakeCartService(cart=cart, raises=raises)
    module._deps = {"cart_service": service}
    return service


def _event(user_id="sub-123", body=None) -> dict:
    event = {
        "headers": {"x-request-id": "corr-1"},
        "pathParameters": {"userId": user_id} if user_id else None,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


_LINE = LineItem(
    id="li-1", product_id="cow-milk", quantity=2, frequency=Frequency.DAILY,
    start_date="2026-09-27", added_at="2026-09-25T00:00:00Z", slot_id="slot-am",
)


def test_internal_get_cart_returns_items_and_version_for_path_user():
    service = _inject(internal_get_cart_handler, cart=Cart(line_items=[_LINE], cart_version=4))

    response = internal_get_cart_handler.handler(_event(), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["cartVersion"] == 4
    assert data["items"][0]["slotId"] == "slot-am"
    assert "quote" not in data
    assert service.calls == [("get", "sub-123")]


def test_internal_get_cart_missing_user_is_400():
    _inject(internal_get_cart_handler)

    response = internal_get_cart_handler.handler(_event(user_id=None), None)

    assert response["statusCode"] == 400


def test_internal_remove_items_forwards_body_to_service():
    service = _inject(internal_remove_items_handler, cart=Cart(cart_version=5))

    response = internal_remove_items_handler.handler(
        _event(body={"itemIds": ["li-1"], "ifVersion": 4, "reason": "CHECKOUT",
                     "checkoutId": "chk_1"}),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["cartVersion"] == 5
    assert service.calls == [("remove", "sub-123", ["li-1"], 4, "chk_1")]


def test_internal_remove_items_empty_item_list_is_400():
    service = _inject(internal_remove_items_handler)

    response = internal_remove_items_handler.handler(
        _event(body={"itemIds": [], "ifVersion": 4}), None
    )

    assert response["statusCode"] == 400
    assert service.calls == []


def test_internal_remove_items_stale_version_is_409():
    _inject(internal_remove_items_handler, raises=CartVersionMismatchError("stale"))

    response = internal_remove_items_handler.handler(
        _event(body={"itemIds": ["li-1"], "ifVersion": 1}), None
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["data"]["errorCode"] == "CART_VERSION_MISMATCH"
