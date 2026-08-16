import pytest

from domain.catalog_service import CatalogService
from domain.exceptions import ProductNotFoundError
from domain.models import Category, Product, StockState


class FakeProductRepository:
    def __init__(self, categories=None, products=None, product=None, search_results=None):
        self.categories = categories or []
        self.products = products or []
        self.product = product
        self.search_results = search_results or []
        self.requested_category_ids: list[str] = []
        self.search_calls: list[tuple] = []
        self.stock_change_calls: list[tuple] = []
        self.apply_stock_change_returns = True

    def get_categories(self):
        return self.categories

    def get_products(self, category_id):
        self.requested_category_ids.append(category_id)
        return self.products

    def get_product(self, product_id):
        return self.product

    def search(self, query, filters, sort):
        self.search_calls.append((query, filters, sort))
        return self.search_results

    def apply_stock_change(
        self, product_id, event_id, stock_state, available_from, occurred_at=None
    ):
        self.stock_change_calls.append(
            (product_id, event_id, stock_state, available_from, occurred_at)
        )
        return self.apply_stock_change_returns


_MILK = Category(id="milk", name="Fresh Milk", icon_name="milk")
_COW_MILK = Product(
    id="cow-milk",
    category_id="milk",
    name="Cow Milk",
    description="",
    unit="1L Bottle",
    price_b2c=68,
    price_b2b=None,
    image_url=None,
    tag=None,
    subscription_eligible=False,
    is_veg=True,
    is_organic=False,
    stock_state=StockState.IN_STOCK,
    available_from=None,
)


def test_get_categories_delegates_to_repository():
    repo = FakeProductRepository(categories=[_MILK])
    service = CatalogService(repo)

    assert service.get_categories() == [_MILK]


def test_get_products_delegates_with_category_id():
    repo = FakeProductRepository(products=[_COW_MILK])
    service = CatalogService(repo)

    result = service.get_products("milk")

    assert result == [_COW_MILK]
    assert repo.requested_category_ids == ["milk"]


def test_get_product_raises_not_found_for_missing_product():
    repo = FakeProductRepository(product=None)
    service = CatalogService(repo)

    with pytest.raises(ProductNotFoundError):
        service.get_product("nonexistent")


def test_get_product_returns_the_product_when_found():
    repo = FakeProductRepository(product=_COW_MILK)
    service = CatalogService(repo)

    assert service.get_product("cow-milk") == _COW_MILK


def test_search_delegates_query_filters_sort():
    repo = FakeProductRepository(search_results=[_COW_MILK])
    service = CatalogService(repo)

    result = service.search("cow", None, None)

    assert result == [_COW_MILK]
    assert repo.search_calls == [("cow", None, None)]


def test_apply_stock_change_delegates_and_returns_repository_result():
    repo = FakeProductRepository()
    repo.apply_stock_change_returns = False
    service = CatalogService(repo)

    result = service.apply_stock_change("cow-milk", "evt-1", "OUT_OF_STOCK", None)

    assert result is False
    assert repo.stock_change_calls == [("cow-milk", "evt-1", "OUT_OF_STOCK", None, None)]
