"""Unit tests for GET /v1/inventory/{productId}, POST /v1/inventory/
reserve|commit|release — FastAPI TestClient against a fake
InventoryStockService (same style as test_serviceability_handlers.py)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from domain.exceptions import (
    InsufficientStockError,
    ProductNotFoundError,
    ReservationNotFoundError,
)
from domain.models import Reservation, ReservationStatus, StockState, StockSummary
from handlers.app import app
from handlers.dependencies import get_inventory_stock_service


class FakeService:
    def __init__(self):
        self.summary_result = None
        self.summary_raises = None
        self.reserve_result = None
        self.reserve_raises = None
        self.commit_result = None
        self.commit_raises = None
        self.release_result = None
        self.release_raises = None
        self.calls: list[tuple] = []

    def get_summary(self, product_id):
        self.calls.append(("get_summary", product_id))
        if self.summary_raises:
            raise self.summary_raises
        return self.summary_result

    def reserve(self, product_id, order_ref, quantity, ttl_seconds):
        self.calls.append(("reserve", product_id, order_ref, quantity, ttl_seconds))
        if self.reserve_raises:
            raise self.reserve_raises
        return self.reserve_result

    def commit(self, product_id, order_ref):
        self.calls.append(("commit", product_id, order_ref))
        if self.commit_raises:
            raise self.commit_raises
        return self.commit_result

    def release(self, product_id, order_ref):
        self.calls.append(("release", product_id, order_ref))
        if self.release_raises:
            raise self.release_raises
        return self.release_result


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def fake_service():
    return FakeService()


@pytest.fixture
def client(fake_service):
    app.dependency_overrides[get_inventory_stock_service] = lambda: fake_service
    return TestClient(app)


def _reservation(**overrides):
    defaults = dict(
        id="res-1", product_id="p1", order_ref="order-1", quantity=3,
        status=ReservationStatus.RESERVED, created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    defaults.update(overrides)
    return Reservation(**defaults)


def test_get_inventory_returns_summary(client, fake_service):
    fake_service.summary_result = StockSummary(
        product_id="p1", on_hand=10, reserved=2, available=8,
        low_stock_threshold=5, stock_state=StockState.IN_STOCK,
    )

    response = client.get("/v1/inventory/p1")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] == 8
    assert data["stockState"] == "IN_STOCK"


def test_get_inventory_unknown_product_returns_404(client, fake_service):
    fake_service.summary_raises = ProductNotFoundError("unknown")

    response = client.get("/v1/inventory/unknown")

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "PRODUCT_NOT_FOUND"


def test_reserve_success(client, fake_service):
    fake_service.reserve_result = _reservation(quantity=3)

    response = client.post(
        "/v1/inventory/reserve",
        json={"productId": "p1", "quantity": 3, "orderId": "order-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "RESERVED"
    assert fake_service.calls == [("reserve", "p1", "order-1", 3, None)]


def test_reserve_insufficient_stock_returns_409(client, fake_service):
    fake_service.reserve_raises = InsufficientStockError("not enough")

    response = client.post(
        "/v1/inventory/reserve",
        json={"productId": "p1", "quantity": 999, "orderId": "order-1"},
    )

    assert response.status_code == 409
    assert response.json()["data"]["errorCode"] == "INSUFFICIENT_STOCK"


def test_reserve_rejects_non_positive_quantity_at_the_schema_level(client):
    response = client.post(
        "/v1/inventory/reserve",
        json={"productId": "p1", "quantity": 0, "orderId": "order-1"},
    )

    assert response.status_code == 422


def test_reserve_unknown_product_returns_404(client, fake_service):
    fake_service.reserve_raises = ProductNotFoundError("unknown")

    response = client.post(
        "/v1/inventory/reserve",
        json={"productId": "unknown", "quantity": 1, "orderId": "order-1"},
    )

    assert response.status_code == 404


def test_commit_success(client, fake_service):
    fake_service.commit_result = _reservation(status=ReservationStatus.COMMITTED)

    response = client.post(
        "/v1/inventory/commit", json={"productId": "p1", "orderId": "order-1"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "COMMITTED"


def test_commit_unknown_order_ref_returns_404(client, fake_service):
    fake_service.commit_raises = ReservationNotFoundError("no such reservation")

    response = client.post(
        "/v1/inventory/commit", json={"productId": "p1", "orderId": "no-such"}
    )

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "RESERVATION_NOT_FOUND"


def test_release_success(client, fake_service):
    fake_service.release_result = _reservation(status=ReservationStatus.RELEASED)

    response = client.post(
        "/v1/inventory/release", json={"productId": "p1", "orderId": "order-1"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "RELEASED"
