"""Unit tests for PATCH /v1/inventory (MA-119), POST /v1/inventory/
receive, GET /v1/inventory/{productId}/batches, GET /v1/inventory,
GET /v1/inventory/{productId}/audit-log (MA-150) — FastAPI TestClient
against a fake InventoryStockService, exercising the Ops-role admin
authorizer contract (admin_context.py) via the X-Admin-* headers a real
API Gateway parameter mapping would forward."""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from domain.exceptions import (
    AvailableFloorViolationError,
    OnHandFloorViolationError,
    ProductNotFoundError,
    ValidationError,
)
from domain.models import AuditLogEntry, Page, Stock, StockBatch, StockState, StockSummary
from handlers.app import app
from handlers.dependencies import get_inventory_stock_service

OPS_HEADERS = {"X-Admin-Id": "admin-1", "X-Admin-Email": "ops@milkful.test", "X-Admin-Role": "Ops"}
NON_OPS_HEADERS = {"X-Admin-Id": "admin-2", "X-Admin-Email": "support@milkful.test", "X-Admin-Role": "Support"}


class FakeService:
    def __init__(self):
        self.adjust_result = None
        self.adjust_raises = None
        self.adjust_calls: list[tuple] = []
        self.receive_result = None
        self.receive_raises = None
        self.batches_result: list[StockBatch] = []
        self.batches_raises = None
        self.list_result: Page | None = None
        self.audit_result: Page | None = None
        self.audit_raises = None

    def adjust(self, product_id, admin_id, adjustment, reason):
        self.adjust_calls.append((product_id, admin_id, adjustment, reason))
        if self.adjust_raises:
            raise self.adjust_raises
        return self.adjust_result

    def receive(self, product_id, quantity, expiry_date, admin_id, reason, available_from=None):
        if self.receive_raises:
            raise self.receive_raises
        return self.receive_result

    def get_batches(self, product_id):
        if self.batches_raises:
            raise self.batches_raises
        return self.batches_result

    def list_stock(self, status_filter, page, page_size):
        return self.list_result

    def get_audit_log(self, product_id, page, page_size):
        if self.audit_raises:
            raise self.audit_raises
        return self.audit_result


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


def _stock(**overrides):
    defaults = dict(product_id="p1", on_hand=10, reserved=2, low_stock_threshold=5)
    defaults.update(overrides)
    return Stock(**defaults)


def _audit_entry(**overrides):
    defaults = dict(
        id="audit-1", product_id="p1", admin_id="admin-1", previous_quantity=10,
        new_quantity=15, adjustment=5, reason="recount", created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return AuditLogEntry(**defaults)


# --- authz, shared across all five routes --------------------------------


def test_adjust_without_admin_headers_returns_401(client):
    response = client.patch("/v1/inventory", json={"productId": "p1", "adjustment": 5})

    assert response.status_code == 401
    assert response.json()["data"]["errorCode"] == "UNAUTHENTICATED"


def test_adjust_with_non_ops_role_returns_403(client, fake_service):
    response = client.patch(
        "/v1/inventory",
        json={"productId": "p1", "adjustment": 5},
        headers=NON_OPS_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["data"]["errorCode"] == "FORBIDDEN"
    assert fake_service.adjust_calls == []  # never reached the domain layer


# --- MA-119 PATCH /inventory -----------------------------------------------


def test_adjust_success_uses_admin_id_from_header_not_body(client, fake_service):
    fake_service.adjust_result = (_stock(on_hand=15), _audit_entry())

    response = client.patch(
        "/v1/inventory",
        json={"productId": "p1", "adjustment": 5, "reason": "recount", "adminId": "spoofed"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 200
    assert fake_service.adjust_calls == [("p1", "admin-1", 5, "recount")]  # not "spoofed"
    assert response.json()["data"]["stock"]["onHand"] == 15


def test_adjust_on_hand_floor_violation_returns_400_with_specific_code(client, fake_service):
    fake_service.adjust_raises = OnHandFloorViolationError("would go negative")

    response = client.patch(
        "/v1/inventory", json={"productId": "p1", "adjustment": -100}, headers=OPS_HEADERS
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "ON_HAND_FLOOR_VIOLATION"


def test_adjust_available_floor_violation_returns_a_distinct_code(client, fake_service):
    fake_service.adjust_raises = AvailableFloorViolationError("reserved stock at risk")

    response = client.patch(
        "/v1/inventory", json={"productId": "p1", "adjustment": -20}, headers=OPS_HEADERS
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "AVAILABLE_FLOOR_VIOLATION"


def test_adjust_unknown_product_returns_404(client, fake_service):
    fake_service.adjust_raises = ProductNotFoundError("unknown")

    response = client.patch(
        "/v1/inventory", json={"productId": "unknown", "adjustment": 5}, headers=OPS_HEADERS
    )

    assert response.status_code == 404


# --- MA-150 FR-1: receive ----------------------------------------------------


def test_receive_success_returns_201(client, fake_service):
    batch = StockBatch(
        id="batch-1", product_id="p1", quantity=10, expiry_date=date(2026, 12, 25),
        available_from=None,
    )
    fake_service.receive_result = (batch, _stock(on_hand=20), _audit_entry(reason="goods_receipt"))

    response = client.post(
        "/v1/inventory/receive",
        json={"productId": "p1", "quantity": 10, "expiryDate": "2026-12-25"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["data"]["batch"]["quantity"] == 10


def test_receive_validation_error_returns_400(client, fake_service):
    fake_service.receive_raises = ValidationError("expiryDate must not be in the past")

    response = client.post(
        "/v1/inventory/receive",
        json={"productId": "p1", "quantity": 10, "expiryDate": "2020-01-01"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "VALIDATION_ERROR"


def test_receive_unknown_product_returns_404(client, fake_service):
    fake_service.receive_raises = ProductNotFoundError("unknown")

    response = client.post(
        "/v1/inventory/receive",
        json={"productId": "unknown", "quantity": 10, "expiryDate": "2026-12-25"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 404


# --- MA-150 FR-2: batches ------------------------------------------------------


def test_get_batches_returns_oldest_expiry_first(client, fake_service):
    fake_service.batches_result = [
        StockBatch(id="b1", product_id="p1", quantity=5, expiry_date=date(2026, 9, 1), available_from=None),
        StockBatch(id="b2", product_id="p1", quantity=5, expiry_date=date(2026, 12, 1), available_from=None),
    ]

    response = client.get("/v1/inventory/p1/batches", headers=OPS_HEADERS)

    assert response.status_code == 200
    assert [b["batchId"] for b in response.json()["data"]["items"]] == ["b1", "b2"]


def test_get_batches_empty_list_is_200_not_404(client, fake_service):
    fake_service.batches_result = []

    response = client.get("/v1/inventory/p1/batches", headers=OPS_HEADERS)

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_get_batches_unknown_product_returns_404(client, fake_service):
    fake_service.batches_raises = ProductNotFoundError("unknown")

    response = client.get("/v1/inventory/unknown/batches", headers=OPS_HEADERS)

    assert response.status_code == 404


# --- MA-150 FR-3: list ----------------------------------------------------------


def test_list_inventory_returns_page(client, fake_service):
    fake_service.list_result = Page(
        items=[
            StockSummary(
                product_id="p1", on_hand=0, reserved=0, available=0,
                low_stock_threshold=5, stock_state=StockState.OUT_OF_STOCK,
            )
        ],
        total=1, page=1, page_size=50,
    )

    response = client.get("/v1/inventory", params={"stockState": "OUT_OF_STOCK"}, headers=OPS_HEADERS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["stockState"] == "OUT_OF_STOCK"


def test_list_inventory_rejects_unknown_stock_state(client, fake_service):
    response = client.get("/v1/inventory", params={"stockState": "BOGUS"}, headers=OPS_HEADERS)

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "VALIDATION_ERROR"


# --- MA-150 FR-4: audit log -----------------------------------------------------


def test_get_audit_log_returns_newest_first_page(client, fake_service):
    fake_service.audit_result = Page(
        items=[_audit_entry(reason="second"), _audit_entry(reason="first")], total=2, page=1, page_size=50
    )

    response = client.get("/v1/inventory/p1/audit-log", headers=OPS_HEADERS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert [e["reason"] for e in data["items"]] == ["second", "first"]


def test_get_audit_log_unknown_product_returns_404(client, fake_service):
    fake_service.audit_raises = ProductNotFoundError("unknown")

    response = client.get("/v1/inventory/unknown/audit-log", headers=OPS_HEADERS)

    assert response.status_code == 404
