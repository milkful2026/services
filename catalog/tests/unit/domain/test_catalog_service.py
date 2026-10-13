import pytest

from domain.catalog_service import CatalogService
from domain.exceptions import CategoryNotFoundError, ProductNotFoundError, ValidationError
from domain.models import Category, Product, StockState


class FakeProductRepository:
    def __init__(
        self,
        categories=None,
        products=None,
        product=None,
        search_results=None,
        existing_category_ids=None,
    ):
        self.categories = categories or []
        self.products = products or []
        self.product = product
        self.search_results = search_results or []
        self.existing_category_ids = (
            {"milk"} if existing_category_ids is None else existing_category_ids
        )
        self.requested_category_ids: list[str] = []
        self.search_calls: list[tuple] = []
        self.stock_change_calls: list[tuple] = []
        self.apply_stock_change_returns = True
        self.create_product_calls: list[tuple] = []
        self.update_product_calls: list[tuple] = []
        self.created_product = product
        self.updated_product = product

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

    def category_exists(self, category_id):
        return category_id in self.existing_category_ids

    def create_product(self, product_id, admin_id, reason, **fields):
        self.create_product_calls.append((product_id, admin_id, reason, fields))
        return self.created_product

    def update_product(self, product_id, admin_id, reason, **changes):
        self.update_product_calls.append((product_id, admin_id, reason, changes))
        return self.updated_product

    def create_category(self, category_id, **fields):
        return Category(id=category_id, name=fields["name"], icon_name=fields["icon_name"])

    def update_category(self, category_id, **changes):
        return Category(id=category_id, name=changes.get("name", "x"), icon_name="icon")

    def list_products_admin(self, category_id, is_active, page, page_size):
        return None

    def get_audit_log(self, product_id, page, page_size):
        return None


class FakeEventPublisher:
    def __init__(self):
        self.published: list[str] = []

    def publish_catalog_updated(self, product_id):
        self.published.append(product_id)


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


# --- MA-156 (MA-42): admin write API ----------------------------------------


def test_create_product_rejects_unknown_category():
    repo = FakeProductRepository(existing_category_ids=set())
    service = CatalogService(repo)

    with pytest.raises(CategoryNotFoundError):
        service.create_product("admin-1", None, category_id="milk", name="X", unit="1L", price_b2c=10)

    assert repo.create_product_calls == []


def test_create_product_rejects_non_positive_price():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    with pytest.raises(ValidationError):
        service.create_product("admin-1", None, category_id="milk", name="X", unit="1L", price_b2c=0)

    assert repo.create_product_calls == []


def test_create_product_rejects_empty_name_or_unit():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    with pytest.raises(ValidationError):
        service.create_product("admin-1", None, category_id="milk", name="  ", unit="1L", price_b2c=10)
    with pytest.raises(ValidationError):
        service.create_product("admin-1", None, category_id="milk", name="X", unit=" ", price_b2c=10)


def test_create_product_delegates_to_repository_and_publishes_catalog_updated():
    repo = FakeProductRepository(product=_COW_MILK)
    publisher = FakeEventPublisher()
    service = CatalogService(repo, publisher)

    result = service.create_product(
        "admin-1", "new SKU", category_id="milk", name="X", unit="1L", price_b2c=10
    )

    assert result == _COW_MILK
    assert len(repo.create_product_calls) == 1
    _, admin_id, reason, fields = repo.create_product_calls[0]
    assert admin_id == "admin-1"
    assert reason == "new SKU"
    assert fields["category_id"] == "milk"
    assert publisher.published == [_COW_MILK.id]


def test_create_product_with_no_publisher_does_not_raise():
    repo = FakeProductRepository(product=_COW_MILK)
    service = CatalogService(repo)  # no event_publisher passed

    service.create_product("admin-1", None, category_id="milk", name="X", unit="1L", price_b2c=10)


def test_update_product_rejects_non_positive_price():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    with pytest.raises(ValidationError):
        service.update_product("cow-milk", "admin-1", None, price_b2c=-5)

    assert repo.update_product_calls == []


@pytest.mark.parametrize(
    "field",
    [
        "category_id",
        "name",
        "description",
        "unit",
        "price_b2c",
        "subscription_eligible",
        "is_veg",
        "is_organic",
        "is_active",
    ],
)
def test_update_product_rejects_explicit_null_for_not_nullable_fields(field):
    # Regression (code review finding): an explicit JSON `null` for a
    # NOT NULL column was previously indistinguishable from "field
    # omitted" (validation skipped on None), reaching the DB as `SET
    # col = NULL` and surfacing as a 503 constraint-violation error
    # instead of a 400.
    repo = FakeProductRepository()
    service = CatalogService(repo)

    with pytest.raises(ValidationError):
        service.update_product("cow-milk", "admin-1", None, **{field: None})

    assert repo.update_product_calls == []


def test_update_product_allows_null_for_nullable_fields():
    repo = FakeProductRepository(product=_COW_MILK)
    service = CatalogService(repo)

    service.update_product(
        "cow-milk", "admin-1", None, price_b2b=None, image_url=None, tag=None,
        hsn_code=None, tax_rate=None,
    )

    assert len(repo.update_product_calls) == 1


def test_update_category_rejects_explicit_null_for_not_nullable_fields():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    with pytest.raises(ValidationError):
        service.update_category("milk", name=None)


def test_update_product_rejects_unknown_category():
    repo = FakeProductRepository(existing_category_ids=set())
    service = CatalogService(repo)

    with pytest.raises(CategoryNotFoundError):
        service.update_product("cow-milk", "admin-1", None, category_id="curd")


def test_update_product_does_not_publish_catalog_updated():
    repo = FakeProductRepository(product=_COW_MILK)
    publisher = FakeEventPublisher()
    service = CatalogService(repo, publisher)

    service.update_product("cow-milk", "admin-1", "price change", price_b2c=75)

    assert publisher.published == []


def test_update_product_allows_isactive_without_other_fields():
    repo = FakeProductRepository(product=_COW_MILK)
    service = CatalogService(repo)

    service.update_product("cow-milk", "admin-1", "deactivate", is_active=False)

    assert repo.update_product_calls[0][3] == {"is_active": False}


def test_create_category_delegates_to_repository():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    category = service.create_category("New Cat", "icon", 5)

    assert category.name == "New Cat"
    assert category.icon_name == "icon"


def test_update_category_delegates_to_repository():
    repo = FakeProductRepository()
    service = CatalogService(repo)

    category = service.update_category("milk", name="Renamed")

    assert category.name == "Renamed"
