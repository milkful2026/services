from datetime import UTC, date, datetime

import pytest

from adapters.product_repository import SqlAlchemyProductRepository, _to_wire_keys
from domain.exceptions import CategoryNotFoundError, ProductNotFoundError
from domain.models import SearchFilters, SortOrder, StockState
from tests.conftest import seed_category, seed_product


def test_get_categories_returns_sorted_by_sort_order(sqlite_engine):
    seed_category(sqlite_engine, id="curd", name="Yogurt & Curd", sort_order=1)
    seed_category(sqlite_engine, id="milk", name="Fresh Milk", sort_order=0)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    categories = repo.get_categories()

    assert [c.id for c in categories] == ["milk", "curd"]


def test_get_products_filters_by_category(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    seed_category(sqlite_engine, id="curd", name="Yogurt & Curd")
    seed_product(sqlite_engine, id="cow-milk", category_id="milk")
    seed_product(sqlite_engine, id="set-curd", category_id="curd", name="Set Curd")
    repo = SqlAlchemyProductRepository(sqlite_engine)

    products = repo.get_products("milk")

    assert [p.id for p in products] == ["cow-milk"]


def test_get_product_returns_none_for_unknown_id(sqlite_engine):
    seed_category(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    assert repo.get_product("nonexistent") is None


def test_get_product_maps_all_fields(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(
        sqlite_engine,
        stock_state="AVAILABLE_FROM",
        available_from=date(2026, 9, 1),
        price_b2b=60,
    )
    repo = SqlAlchemyProductRepository(sqlite_engine)

    product = repo.get_product("cow-milk")

    assert product.name == "Cow Milk"
    assert product.stock_state == StockState.AVAILABLE_FROM
    assert product.available_from == date(2026, 9, 1)
    assert product.price_b2b == 60.0
    assert product.is_organic is True


def test_search_matches_name_or_description(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="cow-milk", name="Cow Milk", description="dairy")
    seed_product(sqlite_engine, id="buffalo-milk", name="Buffalo Milk", description="dairy")
    seed_product(sqlite_engine, id="ghee", name="Cow Ghee", description="clarified butter")
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search("cow", None, None)

    assert {p.id for p in results} == {"cow-milk", "ghee"}


def test_search_category_filter_combines_as_or(sqlite_engine):
    # MA-115 FR-6's own open question (Q6), resolved: repeated category
    # filters are OR'd, not AND'd (a product has exactly one category).
    seed_category(sqlite_engine, id="milk")
    seed_category(sqlite_engine, id="curd", name="Yogurt & Curd")
    seed_product(sqlite_engine, id="cow-milk", category_id="milk")
    seed_product(sqlite_engine, id="set-curd", category_id="curd", name="Set Curd")
    seed_product(sqlite_engine, id="ghee", category_id="milk", name="Ghee")
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search(None, SearchFilters(category_ids=["milk", "curd"]), None)

    assert {p.id for p in results} == {"cow-milk", "set-curd", "ghee"}


def test_search_price_range_filter(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="cheap", price_b2c=30)
    seed_product(sqlite_engine, id="mid", price_b2c=70)
    seed_product(sqlite_engine, id="pricey", price_b2c=500)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search(None, SearchFilters(category_ids=[], min_price=50, max_price=100), None)

    assert [p.id for p in results] == ["mid"]


def test_search_veg_and_organic_filters(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="veg-organic", is_veg=True, is_organic=True)
    seed_product(sqlite_engine, id="veg-only", is_veg=True, is_organic=False)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search(None, SearchFilters(category_ids=[], organic_only=True), None)

    assert [p.id for p in results] == ["veg-organic"]


def test_search_sort_price_asc_and_desc(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="mid", price_b2c=70)
    seed_product(sqlite_engine, id="cheap", price_b2c=30)
    seed_product(sqlite_engine, id="pricey", price_b2c=500)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    asc = repo.search(None, None, SortOrder.PRICE_ASC)
    desc = repo.search(None, None, SortOrder.PRICE_DESC)

    assert [p.id for p in asc] == ["cheap", "mid", "pricey"]
    assert [p.id for p in desc] == ["pricey", "mid", "cheap"]


def test_search_sort_newest_orders_by_created_at_desc(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="old", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    seed_product(sqlite_engine, id="new", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search(None, None, SortOrder.NEWEST)

    assert [p.id for p in results] == ["new", "old"]


def test_apply_stock_change_updates_state(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    applied = repo.apply_stock_change(
        "cow-milk", event_id="evt-1", stock_state="OUT_OF_STOCK", available_from=None
    )

    assert applied is True
    product = repo.get_product("cow-milk")
    assert product.stock_state == StockState.OUT_OF_STOCK


def test_apply_stock_change_is_idempotent_on_duplicate_event_id(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)
    repo.apply_stock_change(
        "cow-milk", event_id="evt-1", stock_state="OUT_OF_STOCK", available_from=None
    )

    # Same eventId redelivered — must not re-apply.
    applied_again = repo.apply_stock_change(
        "cow-milk", event_id="evt-1", stock_state="IN_STOCK", available_from=None
    )

    assert applied_again is False
    product = repo.get_product("cow-milk")
    assert product.stock_state == StockState.OUT_OF_STOCK


def test_apply_stock_change_rejects_a_stale_out_of_order_event(sqlite_engine):
    # A *distinct* eventId can still be a stale, out-of-order redelivery
    # (SQS gives no ordering guarantee) — must not overwrite a newer state
    # with one that occurred earlier, even though its eventId is new.
    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)
    newer = datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)
    older = datetime(2026, 8, 16, 9, 0, 0, tzinfo=UTC)
    repo.apply_stock_change(
        "cow-milk",
        event_id="evt-2",
        stock_state="OUT_OF_STOCK",
        available_from=None,
        occurred_at=newer,
    )

    applied_late = repo.apply_stock_change(
        "cow-milk",
        event_id="evt-1",
        stock_state="IN_STOCK",
        available_from=None,
        occurred_at=older,
    )

    assert applied_late is False
    product = repo.get_product("cow-milk")
    assert product.stock_state == StockState.OUT_OF_STOCK


def test_apply_stock_change_accepts_a_later_event_after_an_earlier_one(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)
    earlier = datetime(2026, 8, 16, 9, 0, 0, tzinfo=UTC)
    later = datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)
    repo.apply_stock_change(
        "cow-milk",
        event_id="evt-1",
        stock_state="OUT_OF_STOCK",
        available_from=None,
        occurred_at=earlier,
    )

    applied = repo.apply_stock_change(
        "cow-milk",
        event_id="evt-2",
        stock_state="IN_STOCK",
        available_from=None,
        occurred_at=later,
    )

    assert applied is True
    product = repo.get_product("cow-milk")
    assert product.stock_state == StockState.IN_STOCK


def test_apply_stock_change_for_unknown_product_is_a_noop(sqlite_engine):
    seed_category(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    applied = repo.apply_stock_change(
        "nonexistent", event_id="evt-1", stock_state="OUT_OF_STOCK", available_from=None
    )

    assert applied is False


# --- MA-156 (MA-42): FR-7 is_active filtering ------------------------------


def test_get_products_filters_out_inactive(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="active-one", is_active=True)
    seed_product(sqlite_engine, id="inactive-one", name="Inactive", is_active=False)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    products = repo.get_products("milk")

    assert [p.id for p in products] == ["active-one"]


def test_get_product_does_not_filter_on_is_active(sqlite_engine):
    # FR-7: a direct-id lookup must keep resolving for an old
    # cart/order/subscription reference even after deactivation.
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, is_active=False)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    product = repo.get_product("cow-milk")

    assert product is not None
    assert product.is_active is False


def test_search_filters_out_inactive(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine, id="active-one", is_active=True)
    seed_product(sqlite_engine, id="inactive-one", name="Inactive", is_active=False)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    results = repo.search(None, None, None)

    assert [p.id for p in results] == ["active-one"]


# --- MA-156 (MA-42): admin write API ---------------------------------------


def test_to_wire_keys_translates_known_columns():
    assert _to_wire_keys({"price_b2c": 10, "is_veg": True}) == {"priceB2c": 10, "isVeg": True}


def test_to_wire_keys_falls_back_to_the_snake_case_key_for_an_unmapped_column():
    # Regression (code review finding): `_COLUMN_TO_WIRE` is hand-
    # maintained and has to stay in sync with `products_table` and
    # catalog_service.py's writable-fields list across multiple files.
    # A future column missing from this mapping must degrade to its own
    # snake_case name, not raise an uncaught KeyError from inside an
    # open transaction.
    assert _to_wire_keys({"brand": "Acme"}) == {"brand": "Acme"}


def test_category_exists(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    repo = SqlAlchemyProductRepository(sqlite_engine)

    assert repo.category_exists("milk") is True
    assert repo.category_exists("nonexistent") is False


def test_create_product_writes_product_and_create_audit_row_in_one_transaction(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    repo = SqlAlchemyProductRepository(sqlite_engine)

    product = repo.create_product(
        "new-product",
        admin_id="admin-1",
        reason="new SKU",
        category_id="milk",
        name="New Product",
        description="desc",
        unit="1L",
        price_b2c=50,
        price_b2b=None,
        image_url=None,
        tag=None,
        subscription_eligible=False,
        is_veg=True,
        is_organic=False,
        hsn_code=None,
        tax_rate=None,
    )

    assert product.id == "new-product"
    assert product.is_active is True

    audit_page = repo.get_audit_log("new-product", page=1, page_size=10)
    assert audit_page.total == 1
    entry = audit_page.items[0]
    assert entry.action == "CREATE"
    assert entry.admin_id == "admin-1"
    assert entry.reason == "new SKU"
    assert entry.previous_value is None
    assert entry.new_value["name"] == "New Product"


def test_update_product_only_records_changed_fields_in_audit_row(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    seed_product(sqlite_engine, id="cow-milk", name="Cow Milk", price_b2c=68)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    product = repo.update_product(
        "cow-milk", admin_id="admin-1", reason="price change", price_b2c=75
    )

    assert product.price_b2c == 75.0
    audit_page = repo.get_audit_log("cow-milk", page=1, page_size=10)
    assert audit_page.total == 1
    entry = audit_page.items[0]
    assert entry.action == "UPDATE"
    assert entry.previous_value == {"priceB2c": 68.0}
    assert entry.new_value == {"priceB2c": 75.0}


def test_update_product_raises_not_found_for_unknown_id(sqlite_engine):
    seed_category(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    with pytest.raises(ProductNotFoundError):
        repo.update_product("nonexistent", admin_id="admin-1", reason=None, price_b2c=10)


def test_create_category_and_update_category(sqlite_engine):
    repo = SqlAlchemyProductRepository(sqlite_engine)

    category = repo.create_category("new-cat", name="New Cat", icon_name="icon", sort_order=5)
    assert category.id == "new-cat"

    updated = repo.update_category("new-cat", name="Renamed")
    assert updated.name == "Renamed"
    assert updated.icon_name == "icon"  # untouched field preserved


def test_update_category_raises_not_found_for_unknown_id(sqlite_engine):
    repo = SqlAlchemyProductRepository(sqlite_engine)

    with pytest.raises(CategoryNotFoundError):
        repo.update_category("nonexistent", name="x")


def test_list_products_admin_includes_inactive(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    seed_product(sqlite_engine, id="active-one", is_active=True)
    seed_product(sqlite_engine, id="inactive-one", name="Inactive", is_active=False)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    page = repo.list_products_admin(category_id=None, is_active=None, page=1, page_size=50)

    assert page.total == 2
    assert {s.id for s in page.items} == {"active-one", "inactive-one"}


def test_list_products_admin_filters_by_category_and_is_active(sqlite_engine):
    seed_category(sqlite_engine, id="milk")
    seed_category(sqlite_engine, id="curd", name="Curd")
    seed_product(sqlite_engine, id="active-milk", category_id="milk", is_active=True)
    seed_product(sqlite_engine, id="inactive-milk", category_id="milk", name="x", is_active=False)
    seed_product(sqlite_engine, id="active-curd", category_id="curd", name="y", is_active=True)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    page = repo.list_products_admin(category_id="milk", is_active=True, page=1, page_size=50)

    assert [s.id for s in page.items] == ["active-milk"]


def test_get_audit_log_raises_not_found_for_unknown_product(sqlite_engine):
    seed_category(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    with pytest.raises(ProductNotFoundError):
        repo.get_audit_log("nonexistent", page=1, page_size=10)


def test_get_audit_log_empty_for_product_with_no_audit_rows(sqlite_engine):
    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    page = repo.get_audit_log("cow-milk", page=1, page_size=10)

    assert page.total == 0
    assert page.items == []


def test_get_audit_log_newest_first(sqlite_engine):
    # Explicit, well-separated created_at values rather than two fast
    # successive update_product() calls — SQLite's CURRENT_TIMESTAMP
    # (func.now()) only has second resolution in the test double, which
    # can't reliably distinguish two calls issued within the same second
    # (same documented fidelity gap as inventory's own
    # test_get_audit_log_newest_first, stock_repository tests). This test
    # is about the ORDER BY clause, not wall-clock timing.
    from adapters.product_repository import catalog_audit_log_table, new_audit_id

    seed_category(sqlite_engine)
    seed_product(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(
            catalog_audit_log_table.insert().values(
                id=new_audit_id(),
                product_id="cow-milk",
                admin_id="admin-1",
                action="UPDATE",
                reason="first",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        conn.execute(
            catalog_audit_log_table.insert().values(
                id=new_audit_id(),
                product_id="cow-milk",
                admin_id="admin-1",
                action="UPDATE",
                reason="second",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
    repo = SqlAlchemyProductRepository(sqlite_engine)

    page = repo.get_audit_log("cow-milk", page=1, page_size=10)

    assert [e.reason for e in page.items] == ["second", "first"]
