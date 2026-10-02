"""Unit tests for SqlAlchemyStockRepository against the SQLite test
double. True lock-contention behavior is NOT verified here (SQLite's
`FOR UPDATE` is a documented no-op, see stock_repository.py's module
docstring) — see tests/concurrency/test_reserve_concurrency.py for the
real-Postgres concurrency/load test (MA-118 §10's highest-priority
test)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from adapters.stock_repository import SqlAlchemyStockRepository
from domain.exceptions import (
    AvailableFloorViolationError,
    InsufficientStockError,
    OnHandFloorViolationError,
    ProductNotFoundError,
    ReservationNotFoundError,
)
from domain.models import ReservationStatus, StockState
from tests.conftest import seed_batch, seed_reservation, seed_stock


@pytest.fixture
def repo(stock_engine):
    return SqlAlchemyStockRepository(stock_engine)


# --- provisioning (FR-8) -----------------------------------------------


def test_provision_stock_if_absent_creates_row(repo, stock_engine):
    created = repo.provision_stock_if_absent("new-product")

    assert created is True
    loaded = repo.get_stock_with_next_batch("new-product")
    assert loaded[0].on_hand == 0
    assert loaded[0].reserved == 0


def test_provision_stock_if_absent_is_idempotent(repo, stock_engine):
    seed_stock(stock_engine, "existing-product", on_hand=5)

    created = repo.provision_stock_if_absent("existing-product")

    assert created is False
    assert repo.get_stock_with_next_batch("existing-product")[0].on_hand == 5


# --- FR-1 read -----------------------------------------------------------


def test_get_stock_with_next_batch_returns_none_for_unknown_product(repo):
    assert repo.get_stock_with_next_batch("unknown") is None


def test_get_stock_with_next_batch_in_stock_has_no_next_batch(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    stock, next_batch = repo.get_stock_with_next_batch("p1")

    assert stock.available == 10
    assert next_batch is None


def test_get_stock_with_next_batch_available_from_when_out_of_stock(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=0, reserved=0)
    seed_batch(stock_engine, "p1", quantity=5, available_from=date(2026, 9, 1))

    stock, next_batch = repo.get_stock_with_next_batch("p1")

    assert stock.available == 0
    assert next_batch.available_from == date(2026, 9, 1)


# --- FR-2: reserve ---------------------------------------------------------


def test_reserve_decrements_available_increments_reserved(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    reservation, created = repo.reserve("p1", "order-1", 3, ttl_seconds=900)

    assert created is True
    assert reservation.quantity == 3
    assert reservation.status == ReservationStatus.RESERVED
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.reserved == 3
    assert stock.available == 7


def test_reserve_insufficient_stock_raises_specific_error(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=2, reserved=0)

    with pytest.raises(InsufficientStockError):
        repo.reserve("p1", "order-1", 3, ttl_seconds=900)

    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.available == 2  # unchanged


def test_reserve_unknown_product_raises_not_found(repo):
    with pytest.raises(ProductNotFoundError):
        repo.reserve("unknown", "order-1", 1, ttl_seconds=900)


def test_reserve_is_idempotent_on_product_and_order_ref(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    first, created_first = repo.reserve("p1", "order-1", 3, ttl_seconds=900)
    second, created_second = repo.reserve("p1", "order-1", 3, ttl_seconds=900)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.reserved == 3  # not double-decremented


# --- FR-3: commit -----------------------------------------------------------


def test_commit_decrements_on_hand_and_clears_reserved(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    seed_batch(stock_engine, "p1", quantity=10, expiry_date=date(2026, 12, 1))
    repo.reserve("p1", "order-1", 4, ttl_seconds=900)

    reservation = repo.commit_reservation("p1", "order-1")

    assert reservation.status == ReservationStatus.COMMITTED
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 6
    assert stock.reserved == 0
    assert stock.available == 6


def test_commit_consumes_batches_fifo_by_expiry(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    near = seed_batch(stock_engine, "p1", quantity=5, expiry_date=date(2026, 9, 1))
    far = seed_batch(stock_engine, "p1", quantity=5, expiry_date=date(2026, 12, 1))
    repo.reserve("p1", "order-1", 7, ttl_seconds=900)

    repo.commit_reservation("p1", "order-1")

    with stock_engine.connect() as conn:
        from adapters.stock_repository import stock_batches_table
        from sqlalchemy import select

        rows = {
            r.id: r.quantity
            for r in conn.execute(select(stock_batches_table)).fetchall()
        }
    assert rows[near["id"]] == 0  # fully drawn first
    assert rows[far["id"]] == 3  # remainder drawn from the later-expiry batch


def test_commit_is_idempotent(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    seed_batch(stock_engine, "p1", quantity=10)
    repo.reserve("p1", "order-1", 4, ttl_seconds=900)
    repo.commit_reservation("p1", "order-1")

    second = repo.commit_reservation("p1", "order-1")

    assert second.status == ReservationStatus.COMMITTED
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 6  # not double-decremented


def test_commit_unknown_order_ref_raises_not_found(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    with pytest.raises(ReservationNotFoundError):
        repo.commit_reservation("p1", "no-such-order")


# --- FR-4: release ----------------------------------------------------------


def test_release_returns_reserved_quantity_to_available(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    repo.reserve("p1", "order-1", 4, ttl_seconds=900)

    reservation = repo.release_reservation("p1", "order-1")

    assert reservation.status == ReservationStatus.RELEASED
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 10  # untouched
    assert stock.reserved == 0
    assert stock.available == 10


def test_release_is_idempotent(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    repo.reserve("p1", "order-1", 4, ttl_seconds=900)
    repo.release_reservation("p1", "order-1")

    second = repo.release_reservation("p1", "order-1")

    assert second.status == ReservationStatus.RELEASED
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.available == 10  # not double-released


def test_release_unknown_order_ref_raises_not_found(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    with pytest.raises(ReservationNotFoundError):
        repo.release_reservation("p1", "no-such-order")


def test_commit_then_release_is_a_noop_not_a_double_mutation(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    seed_batch(stock_engine, "p1", quantity=10)
    repo.reserve("p1", "order-1", 4, ttl_seconds=900)
    repo.commit_reservation("p1", "order-1")

    result = repo.release_reservation("p1", "order-1")

    assert result.status == ReservationStatus.COMMITTED  # terminal state unchanged
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 6
    assert stock.reserved == 0


# --- FR-5: release_by_order_ref ----------------------------------------------


def test_release_by_order_ref_releases_every_product_for_the_order(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    seed_stock(stock_engine, "p2", on_hand=10, reserved=0)
    repo.reserve("p1", "order-1", 2, ttl_seconds=900)
    repo.reserve("p2", "order-1", 3, ttl_seconds=900)

    released = repo.release_by_order_ref("order-1")

    assert {r.product_id for r in released} == {"p1", "p2"}
    assert repo.get_stock_with_next_batch("p1")[0].available == 10
    assert repo.get_stock_with_next_batch("p2")[0].available == 10


def test_release_by_order_ref_is_a_noop_when_nothing_active(repo):
    assert repo.release_by_order_ref("no-such-order") == []


# --- FR-2 TTL sweep -----------------------------------------------------------


def test_sweep_releases_only_expired_reserved_rows(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=5)
    expired = seed_reservation(
        stock_engine, "p1", "order-expired", quantity=3,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    seed_reservation(
        stock_engine, "p1", "order-not-expired", quantity=2,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    released = repo.sweep_expired_reservations()

    assert [r.id for r in released] == [expired["id"]]
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.reserved == 2  # only the expired 3 were given back


def test_sweep_ignores_already_terminal_rows(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    seed_reservation(
        stock_engine, "p1", "order-1", quantity=3, status="RELEASED",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    released = repo.sweep_expired_reservations()

    assert released == []


# --- MA-119 FR-1: adjust ------------------------------------------------------


def test_adjust_increases_on_hand_and_writes_audit_row(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)

    stock, audit = repo.adjust("p1", "admin-1", 5, "recount")

    assert stock.on_hand == 15
    assert audit.previous_quantity == 10
    assert audit.new_quantity == 15
    assert audit.adjustment == 5
    assert audit.admin_id == "admin-1"
    assert audit.reason == "recount"


def test_adjust_rejects_negative_on_hand(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=5, reserved=0)

    with pytest.raises(OnHandFloorViolationError):
        repo.adjust("p1", "admin-1", -10, "spoilage")

    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 5  # unchanged


def test_adjust_rejects_negative_available_even_when_on_hand_stays_nonneg(repo, stock_engine):
    # on_hand=100, reserved=90 — adjusting by -20 leaves on_hand=80 (>=0)
    # but available would go to -10. MA-119 §9/§10's exact example.
    seed_stock(stock_engine, "p1", on_hand=100, reserved=90)

    with pytest.raises(AvailableFloorViolationError):
        repo.adjust("p1", "admin-1", -20, "correction")

    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 100  # unchanged


def test_adjust_unknown_product_raises_not_found(repo):
    with pytest.raises(ProductNotFoundError):
        repo.adjust("unknown", "admin-1", 5, "recount")


# --- MA-150 FR-1: receive ------------------------------------------------------


def test_receive_creates_batch_increments_on_hand_and_audits(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=5, reserved=0)

    batch, stock, audit = repo.receive_stock(
        "p1", 20, date(2026, 12, 25), "admin-1", None
    )

    assert batch.quantity == 20
    assert batch.expiry_date == date(2026, 12, 25)
    assert stock.on_hand == 25
    assert audit.adjustment == 20
    assert audit.reason == "goods_receipt"


def test_receive_prefixes_supplied_reason(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=5, reserved=0)

    _, _, audit = repo.receive_stock("p1", 10, date(2026, 12, 25), "admin-1", "supplier delivery")

    assert audit.reason == "goods_receipt: supplier delivery"


def test_receive_unknown_product_raises_not_found(repo):
    with pytest.raises(ProductNotFoundError):
        repo.receive_stock("unknown", 10, date(2026, 12, 25), "admin-1", None)


def test_two_receipts_same_day_both_create_distinct_batches(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=0, reserved=0)

    repo.receive_stock("p1", 10, date(2026, 12, 25), "admin-1", None)
    repo.receive_stock("p1", 10, date(2026, 12, 25), "admin-1", None)

    batches = repo.get_batches("p1")
    assert len(batches) == 2
    stock, _ = repo.get_stock_with_next_batch("p1")
    assert stock.on_hand == 20


# --- MA-150 FR-2: batches -------------------------------------------------------


def test_get_batches_orders_oldest_expiry_first(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=0, reserved=0)
    far = seed_batch(stock_engine, "p1", quantity=5, expiry_date=date(2026, 12, 1))
    near = seed_batch(stock_engine, "p1", quantity=5, expiry_date=date(2026, 9, 1))

    batches = repo.get_batches("p1")

    assert [b.id for b in batches] == [near["id"], far["id"]]


def test_get_batches_empty_list_for_never_received_product(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=0, reserved=0)

    assert repo.get_batches("p1") == []


def test_get_batches_unknown_product_raises_not_found(repo):
    with pytest.raises(ProductNotFoundError):
        repo.get_batches("unknown")


# --- MA-150 FR-3: list ----------------------------------------------------------


def test_list_stock_filters_by_stock_state(repo, stock_engine):
    seed_stock(stock_engine, "in-stock", on_hand=10, reserved=0)
    seed_stock(stock_engine, "out-of-stock", on_hand=0, reserved=0)

    result = repo.list_stock(StockState.OUT_OF_STOCK, page=1, page_size=50)

    assert [item.product_id for item in result.items] == ["out-of-stock"]
    assert result.total == 1


def test_list_stock_paginates(repo, stock_engine):
    for i in range(5):
        seed_stock(stock_engine, f"p{i}", on_hand=10, reserved=0)

    page1 = repo.list_stock(None, page=1, page_size=2)
    page2 = repo.list_stock(None, page=2, page_size=2)

    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert {i.product_id for i in page1.items} != {i.product_id for i in page2.items}


# --- MA-150 FR-4: audit log ------------------------------------------------------


def test_get_audit_log_newest_first(repo, stock_engine):
    # Explicit, well-separated created_at values rather than two fast
    # successive adjust() calls — SQLite's CURRENT_TIMESTAMP (used by
    # func.now()) only has second resolution in the test double, which
    # can't reliably distinguish two calls issued within the same
    # second (same documented fidelity gap as wallet_repository.py's
    # list_ledger_entries comment). This test is about the ORDER BY
    # clause, not wall-clock timing.
    import uuid

    from adapters.stock_repository import inventory_audit_log_table

    seed_stock(stock_engine, "p1", on_hand=10, reserved=0)
    with stock_engine.begin() as conn:
        conn.execute(
            inventory_audit_log_table.insert().values(
                id=str(uuid.uuid4()), product_id="p1", admin_id="admin-1",
                previous_quantity=10, new_quantity=15, adjustment=5, reason="first",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        conn.execute(
            inventory_audit_log_table.insert().values(
                id=str(uuid.uuid4()), product_id="p1", admin_id="admin-1",
                previous_quantity=15, new_quantity=13, adjustment=-2, reason="second",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )

    page = repo.get_audit_log("p1", page=1, page_size=50)

    assert [e.reason for e in page.items] == ["second", "first"]
    assert page.total == 2


def test_get_audit_log_includes_receive_originated_rows(repo, stock_engine):
    seed_stock(stock_engine, "p1", on_hand=0, reserved=0)
    repo.adjust("p1", "admin-1", 5, "manual")
    repo.receive_stock("p1", 10, date(2026, 12, 25), "admin-1", None)

    page = repo.get_audit_log("p1", page=1, page_size=50)

    reasons = {e.reason for e in page.items}
    assert reasons == {"manual", "goods_receipt"}


def test_get_audit_log_unknown_product_raises_not_found(repo):
    with pytest.raises(ProductNotFoundError):
        repo.get_audit_log("unknown", page=1, page_size=50)
