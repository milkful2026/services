import pytest
from fastapi.testclient import TestClient

from domain.exceptions import ProductNotFoundError, ServiceUnavailableError
from domain.models import Category, Product, SearchFilters, SortOrder, StockState
from handlers.app import app
from handlers.dependencies import get_catalog_service


class FakeCatalogService:
    def __init__(self, categories=None, products=None, product=None, product_error=None,
                 search_results=None):
        self.categories = categories or []
        self.products = products or []
        self.product = product
        self.product_error = product_error
        self.search_results = search_results or []
        self.search_calls: list[tuple] = []

    def get_categories(self):
        return self.categories

    def get_products(self, category_id):
        return self.products

    def get_product(self, product_id):
        if self.product_error:
            raise self.product_error
        return self.product

    def search(self, query, filters, sort):
        self.search_calls.append((query, filters, sort))
        return self.search_results


_MILK = Category(id="milk", name="Fresh Milk", icon_name="milk")
_COW_MILK = Product(
    id="cow-milk",
    category_id="milk",
    name="Cow Milk",
    description="Farm-fresh",
    unit="1L Bottle",
    price_b2c=68,
    price_b2b=60,
    image_url="https://example.com/cow-milk.jpg",
    tag="ORGANIC",
    subscription_eligible=True,
    is_veg=True,
    is_organic=True,
    stock_state=StockState.IN_STOCK,
    available_from=None,
)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _override(fake_service: FakeCatalogService) -> FakeCatalogService:
    app.dependency_overrides[get_catalog_service] = lambda: fake_service
    return fake_service


@pytest.fixture
def client():
    return TestClient(app)


def test_list_categories_returns_bare_array_in_data(client):
    # MA-116 FR-3 / the mobile client's ApiClient.requestList contract —
    # `data` is a bare array here, unlike every other endpoint.
    _override(FakeCatalogService(categories=[_MILK]))

    response = client.get("/categories")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"] == [{"id": "milk", "name": "Fresh Milk", "iconName": "milk"}]


def test_list_products_requires_category_id(client):
    _override(FakeCatalogService(products=[_COW_MILK]))

    response = client.get("/products", params={"categoryId": "milk"})

    assert response.status_code == 200
    products = response.json()["data"]["products"]
    assert products == [
        {
            "id": "cow-milk",
            "categoryId": "milk",
            "name": "Cow Milk",
            "description": "Farm-fresh",
            "unit": "1L Bottle",
            "price": 68,
            "imageUrl": "https://example.com/cow-milk.jpg",
            "tag": "ORGANIC",
            "subscriptionEligible": True,
            "isVeg": True,
            "isOrganic": True,
            "stockState": "IN_STOCK",
            "availableFrom": None,
        }
    ]


def test_list_products_without_category_id_is_a_422(client):
    _override(FakeCatalogService())

    response = client.get("/products")

    assert response.status_code == 422


def test_get_product_by_id(client):
    _override(FakeCatalogService(product=_COW_MILK))

    response = client.get("/products/cow-milk")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == "cow-milk"


def test_get_product_not_found_maps_to_404_with_error_envelope(client):
    _override(FakeCatalogService(product_error=ProductNotFoundError("no such product")))

    response = client.get("/products/nonexistent")

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["data"]["errorCode"] == "PRODUCT_NOT_FOUND"


def test_service_unavailable_maps_to_503(client):
    _override(FakeCatalogService(product_error=ServiceUnavailableError("db down")))

    response = client.get("/products/cow-milk")

    assert response.status_code == 503
    assert response.json()["data"]["errorCode"] == "SERVICE_UNAVAILABLE"


def test_search_parses_query_and_repeated_filters(client):
    service = _override(FakeCatalogService(search_results=[_COW_MILK]))

    response = client.get(
        "/search",
        params=[
            ("q", "cow"),
            ("filters", "category:milk"),
            ("filters", "veg:true"),
            ("filters", "price:20-100"),
            ("sort", "price_asc"),
        ],
    )

    assert response.status_code == 200
    assert response.json()["data"]["products"][0]["id"] == "cow-milk"
    query, filters, sort = service.search_calls[0]
    assert query == "cow"
    assert filters == SearchFilters(
        category_ids=["milk"], min_price=20.0, max_price=100.0, veg_only=True, organic_only=False
    )
    assert sort == SortOrder.PRICE_ASC


def test_search_with_no_params_passes_none_filters_and_sort(client):
    service = _override(FakeCatalogService(search_results=[]))

    response = client.get("/search")

    assert response.status_code == 200
    query, filters, sort = service.search_calls[0]
    assert query is None
    assert filters is None
    assert sort is None


def test_search_invalid_sort_maps_to_400_with_error_envelope(client):
    _override(FakeCatalogService())

    response = client.get("/search", params={"sort": "bogus"})

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["data"]["errorCode"] == "INVALID_REQUEST"


def test_search_invalid_price_filter_maps_to_400_with_error_envelope(client):
    _override(FakeCatalogService())

    response = client.get("/search", params={"filters": "price:abc-100"})

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "INVALID_REQUEST"


def test_healthz_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
