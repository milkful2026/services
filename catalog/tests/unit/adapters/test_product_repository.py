from datetime import date

from adapters.product_repository import SqlAlchemyProductRepository
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

    # Same eventId redelivered — must not re-apply (and, in a real
    # scenario, must not overwrite a *newer* state with a stale replay).
    applied_again = repo.apply_stock_change(
        "cow-milk", event_id="evt-1", stock_state="IN_STOCK", available_from=None
    )

    assert applied_again is False
    product = repo.get_product("cow-milk")
    assert product.stock_state == StockState.OUT_OF_STOCK


def test_apply_stock_change_for_unknown_product_is_a_noop(sqlite_engine):
    seed_category(sqlite_engine)
    repo = SqlAlchemyProductRepository(sqlite_engine)

    applied = repo.apply_stock_change(
        "nonexistent", event_id="evt-1", stock_state="OUT_OF_STOCK", available_from=None
    )

    assert applied is False
