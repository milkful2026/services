
import pytest

import handlers.delete_item_handler as delete_item_handler
from domain.exceptions import LineItemNotFoundError


class FakeCartService:
    def __init__(self, raises=None):
        self.raises = raises
        self.calls: list[dict] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def delete_item(self, user_id, line_item_id):
        self.calls.append({"user_id": user_id, "line_item_id": line_item_id})
        if self.raises:
            raise self.raises


@pytest.fixture(autouse=True)
def _reset_deps():
    delete_item_handler._deps = None
    yield
    delete_item_handler._deps = None


def _inject(raises=None):
    service = FakeCartService(raises=raises)
    delete_item_handler._deps = {"cart_service": service}
    return service


def _event(line_item_id: str | None = "item-1", sub: str | None = "sub-123") -> dict:
    return {
        "headers": {"x-request-id": "corr-1"},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub} if sub else {}}}},
        "pathParameters": {"id": line_item_id} if line_item_id else {},
    }


def test_happy_path_returns_204_with_empty_body():
    service = _inject()

    response = delete_item_handler.handler(_event(), None)

    assert response["statusCode"] == 204
    assert response["body"] == ""
    assert service.calls == [{"user_id": "sub-123", "line_item_id": "item-1"}]


def test_missing_line_item_returns_404():
    _inject(raises=LineItemNotFoundError("no such item"))

    response = delete_item_handler.handler(_event(), None)

    assert response["statusCode"] == 404


def test_missing_path_parameter_returns_400():
    _inject()

    response = delete_item_handler.handler(_event(line_item_id=None), None)

    assert response["statusCode"] == 400


def test_missing_jwt_claims_returns_400():
    _inject()

    response = delete_item_handler.handler(_event(sub=None), None)

    assert response["statusCode"] == 400
