"""Unit tests for POST /products, PATCH /products/{id}, POST /categories,
PATCH /categories/{id}, GET /admin/products, GET /products/{id}/audit-log
(MA-156) — FastAPI TestClient against a fake CatalogService, exercising
the Ops-role admin authorizer contract (admin_context.py) via the
X-Admin-* headers a real API Gateway parameter mapping would forward."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from domain.exceptions import (
    CategoryNotFoundError,
    ProductNotFoundError,
    ValidationError,
)
from domain.models import (
    AdminProductSummary,
    CatalogAuditLogEntry,
    Category,
    Page,
    Product,
    StockState,
)
from handlers.app import app
from handlers.dependencies import get_catalog_service

OPS_HEADERS = {"X-Admin-Id": "admin-1", "X-Admin-Email": "ops@milkful.test", "X-Admin-Role": "Ops"}
SUPER_ADMIN_HEADERS = {
    "X-Admin-Id": "admin-3",
    "X-Admin-Email": "superadmin@milkful.test",
    "X-Admin-Role": "SuperAdmin",
}
NON_OPS_HEADERS = {"X-Admin-Id": "admin-2", "X-Admin-Email": "support@milkful.test", "X-Admin-Role": "Support"}


class FakeService:
    def __init__(self):
        self.create_product_result = None
        self.create_product_raises = None
        self.create_product_calls: list[tuple] = []
        self.update_product_result = None
        self.update_product_raises = None
        self.update_product_calls: list[tuple] = []
        self.create_category_result = None
        self.update_category_result = None
        self.update_category_raises = None
        self.list_result: Page | None = None
        self.audit_result: Page | None = None
        self.audit_raises = None

    def create_product(self, admin_id, reason, **fields):
        self.create_product_calls.append((admin_id, reason, fields))
        if self.create_product_raises:
            raise self.create_product_raises
        return self.create_product_result

    def update_product(self, product_id, admin_id, reason, **changes):
        self.update_product_calls.append((product_id, admin_id, reason, changes))
        if self.update_product_raises:
            raise self.update_product_raises
        return self.update_product_result

    def create_category(self, name, icon_name, sort_order):
        return self.create_category_result

    def update_category(self, category_id, **changes):
        if self.update_category_raises:
            raise self.update_category_raises
        return self.update_category_result

    def list_products_admin(self, category_id, is_active, page, page_size):
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
    app.dependency_overrides[get_catalog_service] = lambda: fake_service
    return TestClient(app)


def _product(**overrides):
    defaults = dict(
        id="new-product",
        category_id="milk",
        name="New Product",
        description="",
        unit="1L",
        price_b2c=50,
        price_b2b=None,
        image_url=None,
        tag=None,
        subscription_eligible=False,
        is_veg=True,
        is_organic=False,
        stock_state=StockState.IN_STOCK,
        available_from=None,
    )
    defaults.update(overrides)
    return Product(**defaults)


def _audit_entry(**overrides):
    defaults = dict(
        id="audit-1",
        product_id="cow-milk",
        admin_id="admin-1",
        action="UPDATE",
        previous_value={"price_b2c": 68.0},
        new_value={"price_b2c": 75.0},
        reason="price change",
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return CatalogAuditLogEntry(**defaults)


# --- authz, shared across all six routes -----------------------------------


def test_create_product_without_admin_headers_returns_401(client):
    response = client.post(
        "/products",
        json={"categoryId": "milk", "name": "X", "unit": "1L", "priceB2c": 10},
    )

    assert response.status_code == 401
    assert response.json()["data"]["errorCode"] == "UNAUTHENTICATED"


def test_create_product_with_non_ops_role_returns_403(client, fake_service):
    response = client.post(
        "/products",
        json={"categoryId": "milk", "name": "X", "unit": "1L", "priceB2c": 10},
        headers=NON_OPS_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["data"]["errorCode"] == "FORBIDDEN"
    assert fake_service.create_product_calls == []


def test_create_product_with_super_admin_role_is_also_allowed_through(client, fake_service):
    fake_service.create_product_result = _product()

    response = client.post(
        "/products",
        json={"categoryId": "milk", "name": "X", "unit": "1L", "priceB2c": 10},
        headers=SUPER_ADMIN_HEADERS,
    )

    assert response.status_code == 201
    assert fake_service.create_product_calls[0][0] == "admin-3"


# --- FR-1: POST /products ----------------------------------------------------


def test_create_product_success_uses_admin_id_from_header_not_body(client, fake_service):
    fake_service.create_product_result = _product()

    response = client.post(
        "/products",
        json={"categoryId": "milk", "name": "X", "unit": "1L", "priceB2c": 10, "reason": "new SKU"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 201
    admin_id, reason, fields = fake_service.create_product_calls[0]
    assert admin_id == "admin-1"
    assert reason == "new SKU"
    assert fields["category_id"] == "milk"
    assert response.json()["data"]["id"] == "new-product"


def test_create_product_unknown_category_returns_404(client, fake_service):
    fake_service.create_product_raises = CategoryNotFoundError("unknown")

    response = client.post(
        "/products",
        json={"categoryId": "unknown", "name": "X", "unit": "1L", "priceB2c": 10},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "CATEGORY_NOT_FOUND"


def test_create_product_non_positive_price_returns_400(client, fake_service):
    fake_service.create_product_raises = ValidationError("priceB2c must be a positive number")

    response = client.post(
        "/products",
        json={"categoryId": "milk", "name": "X", "unit": "1L", "priceB2c": 0},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "VALIDATION_ERROR"


# --- FR-2: PATCH /products/{id} ----------------------------------------------


def test_update_product_only_sends_fields_present_in_body(client, fake_service):
    fake_service.update_product_result = _product(id="cow-milk", price_b2c=75)

    response = client.patch(
        "/products/cow-milk",
        json={"priceB2c": 75, "reason": "price change"},
        headers=OPS_HEADERS,
    )

    assert response.status_code == 200
    product_id, admin_id, reason, changes = fake_service.update_product_calls[0]
    assert product_id == "cow-milk"
    assert admin_id == "admin-1"
    assert reason == "price change"
    assert changes == {"price_b2c": 75}


def test_update_product_isactive_false_is_the_deactivate_path(client, fake_service):
    fake_service.update_product_result = _product(id="cow-milk")

    response = client.patch(
        "/products/cow-milk", json={"isActive": False}, headers=OPS_HEADERS
    )

    assert response.status_code == 200
    _, _, _, changes = fake_service.update_product_calls[0]
    assert changes == {"is_active": False}


def test_update_product_unknown_id_returns_404(client, fake_service):
    fake_service.update_product_raises = ProductNotFoundError("unknown")

    response = client.patch("/products/unknown", json={"priceB2c": 10}, headers=OPS_HEADERS)

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "PRODUCT_NOT_FOUND"


# --- FR-3: categories --------------------------------------------------------


def test_create_category_success(client, fake_service):
    fake_service.create_category_result = Category(id="new-cat", name="New Cat", icon_name="icon")

    response = client.post(
        "/categories", json={"name": "New Cat", "iconName": "icon"}, headers=OPS_HEADERS
    )

    assert response.status_code == 201
    assert response.json()["data"]["id"] == "new-cat"


def test_update_category_unknown_id_returns_404(client, fake_service):
    fake_service.update_category_raises = CategoryNotFoundError("unknown")

    response = client.patch("/categories/unknown", json={"name": "x"}, headers=OPS_HEADERS)

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "CATEGORY_NOT_FOUND"


# --- FR-4: GET /admin/products -----------------------------------------------


def test_list_products_admin_includes_inactive(client, fake_service):
    fake_service.list_result = Page(
        items=[
            AdminProductSummary(
                id="p1", category_id="milk", name="X", price_b2c=10, price_b2b=None,
                is_active=False, stock_state=StockState.OUT_OF_STOCK,
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
        ],
        total=1, page=1, page_size=50,
    )

    response = client.get("/admin/products", headers=OPS_HEADERS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"][0]["isActive"] is False


# --- FR-5: GET /products/{id}/audit-log ---------------------------------------


def test_get_audit_log_returns_newest_first_page(client, fake_service):
    fake_service.audit_result = Page(
        items=[_audit_entry(reason="second"), _audit_entry(reason="first")], total=2, page=1, page_size=50
    )

    response = client.get("/products/cow-milk/audit-log", headers=OPS_HEADERS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert [e["reason"] for e in data["items"]] == ["second", "first"]


def test_get_audit_log_unknown_product_returns_404(client, fake_service):
    fake_service.audit_raises = ProductNotFoundError("unknown")

    response = client.get("/products/unknown/audit-log", headers=OPS_HEADERS)

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "PRODUCT_NOT_FOUND"
