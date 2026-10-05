"""Unit tests for InventoryStockService against a fake repository/
publisher — focused on behavior the service layer itself owns: input
validation, and FR-6's StockChanged/LowStock publish decisions (which
repository-level tests, by design, don't exercise since the repository
has no publisher dependency at all)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from domain.exceptions import ValidationError
from domain.inventory_stock_service import InventoryStockService
from domain.models import (
    AuditLogEntry,
    Page,
    Reservation,
    ReservationStatus,
    Stock,
    StockBatch,
    StockState,
)


class FakeRepository:
    def __init__(self, stock: Stock, next_batch: StockBatch | None = None):
        self.stock = stock
        self.next_batch = next_batch
        self.reserve_result: tuple[Reservation, bool] | None = None
        self.commit_result: Reservation | None = None
        self.release_result: Reservation | None = None
        self.on_hand_after_commit: int | None = None

    def get_stock_with_next_batch(self, product_id):
        return (self.stock, self.next_batch)

    def reserve(self, product_id, order_ref, quantity, ttl_seconds):
        reservation, created = self.reserve_result
        if created:
            self.stock.reserved += quantity
        return reservation, created

    def commit_reservation(self, product_id, order_ref):
        changed = self.on_hand_after_commit is not None
        if changed:
            self.stock.on_hand = self.on_hand_after_commit
        return self.commit_result, changed

    def release_reservation(self, product_id, order_ref):
        return self.release_result

    def release_by_order_ref(self, order_ref):
        return [self.release_result] if self.release_result else []

    def sweep_expired_reservations(self, limit=100):
        return [self.release_result] if self.release_result else []

    def adjust(self, product_id, admin_id, adjustment, reason):
        self.stock.on_hand += adjustment
        return self.stock, AuditLogEntry(
            id="audit-1", product_id=product_id, admin_id=admin_id,
            previous_quantity=self.stock.on_hand - adjustment, new_quantity=self.stock.on_hand,
            adjustment=adjustment, reason=reason,
        )

    def receive_stock(self, product_id, quantity, expiry_date, admin_id, reason, available_from=None):
        self.stock.on_hand += quantity
        batch = StockBatch(
            id="batch-1", product_id=product_id, quantity=quantity,
            expiry_date=expiry_date, available_from=available_from,
        )
        audit = AuditLogEntry(
            id="audit-1", product_id=product_id, admin_id=admin_id,
            previous_quantity=self.stock.on_hand - quantity, new_quantity=self.stock.on_hand,
            adjustment=quantity, reason=f"goods_receipt: {reason}" if reason else "goods_receipt",
        )
        return batch, self.stock, audit

    def get_batches(self, product_id):
        return []

    def list_stock(self, status_filter, page, page_size):
        return Page(items=[], total=0, page=page, page_size=page_size)

    def get_audit_log(self, product_id, page, page_size):
        return Page(items=[], total=0, page=page, page_size=page_size)

    def provision_stock_if_absent(self, product_id):
        return True


class FakePublisher:
    def __init__(self):
        self.stock_changed: list = []
        self.low_stock: list = []

    def publish_stock_changed(self, summary):
        self.stock_changed.append(summary)

    def publish_low_stock(self, summary):
        self.low_stock.append(summary)


def _reservation(**overrides):
    defaults = dict(
        id="res-1", product_id="p1", order_ref="order-1", quantity=3,
        status=ReservationStatus.RESERVED, created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    defaults.update(overrides)
    return Reservation(**defaults)


@pytest.fixture
def publisher():
    return FakePublisher()


def _service(repo, publisher):
    return InventoryStockService(repo, publisher)


# --- validation --------------------------------------------------------


def test_reserve_rejects_non_positive_quantity(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    with pytest.raises(ValidationError):
        service.reserve("p1", "order-1", 0)


def test_adjust_rejects_zero_adjustment(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    with pytest.raises(ValidationError):
        service.adjust("p1", "admin-1", 0, "noop")


def test_receive_rejects_non_positive_quantity(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    with pytest.raises(ValidationError):
        service.receive("p1", 0, date(2026, 12, 25), "admin-1", None)


def test_receive_rejects_past_expiry_date(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    with pytest.raises(ValidationError):
        service.receive("p1", 5, date(2020, 1, 1), "admin-1", None)


def test_receive_rejects_past_available_from(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    with pytest.raises(ValidationError):
        service.receive("p1", 5, date(2026, 12, 25), "admin-1", None, date(2020, 1, 1))


def test_receive_with_no_available_from_defaults_to_available_now(publisher):
    # Regression: receive_stock() previously hard-coded available_from=None
    # with no field anywhere to set it to anything else — the
    # AVAILABLE_FROM stockState (already read by get_summary()/
    # get_batches()) had no write path at all. Confirms the (unchanged)
    # default-omitted case still produces an immediately-available batch.
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    batch, _stock, _audit = service.receive("p1", 5, date(2026, 12, 25), "admin-1", None)

    assert batch.available_from is None


def test_receive_can_schedule_a_future_available_from(publisher):
    repo = FakeRepository(Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5))
    service = _service(repo, publisher)

    batch, _stock, _audit = service.receive(
        "p1", 5, date(2026, 12, 25), "admin-1", None, date(2026, 11, 1)
    )

    assert batch.available_from == date(2026, 11, 1)


# --- FR-6 publish decisions ----------------------------------------------


def test_reserve_publishes_stock_changed_when_created(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.reserve_result = (_reservation(quantity=3), True)
    service = _service(repo, publisher)

    service.reserve("p1", "order-1", 3)

    assert len(publisher.stock_changed) == 1
    assert publisher.stock_changed[0].available == 7


def test_reserve_still_publishes_on_idempotent_replay(publisher):
    # Regression: reserve() previously only published when created=True,
    # so a retry after a failed first-call publish (the first call DID
    # create the reservation, but its publish attempt was lost) could
    # never recover — a retry with the same idempotency key always
    # returns created=False, and the old code silently skipped publishing
    # every time. Every other mutator (commit/release/
    # handle_order_cancelled/run_ttl_sweep) already republishes
    # unconditionally on its own idempotent/no-op path; reserve() now
    # does too, so Catalog's cache can always self-heal on a retry.
    stock = Stock("p1", on_hand=10, reserved=3, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.reserve_result = (_reservation(quantity=3), False)
    service = _service(repo, publisher)

    service.reserve("p1", "order-1", 3)

    assert len(publisher.stock_changed) == 1


def test_reserve_replay_does_not_fabricate_a_low_stock_crossing(publisher):
    # A replay's StockChanged still republishes (above), but it must not
    # pass available_delta=-quantity into the LowStock check a second
    # time — that would derive a fake "available before this call" value
    # and could fire a duplicate/spurious LowStock event on every retry,
    # even when nothing has actually changed since the original call.
    stock = Stock("p1", on_hand=10, reserved=8, low_stock_threshold=5)  # available=2, already low
    repo = FakeRepository(stock)
    repo.reserve_result = (_reservation(quantity=3), False)
    service = _service(repo, publisher)

    service.reserve("p1", "order-1", 3)

    assert publisher.low_stock == []


def test_reserve_crossing_threshold_also_publishes_low_stock(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.reserve_result = (_reservation(quantity=8), True)
    service = _service(repo, publisher)

    service.reserve("p1", "order-1", 8)

    assert len(publisher.stock_changed) == 1
    assert len(publisher.low_stock) == 1  # available went 10 -> 2, crossing threshold 5


def test_reserve_not_crossing_threshold_does_not_publish_low_stock(publisher):
    stock = Stock("p1", on_hand=20, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.reserve_result = (_reservation(quantity=3), True)
    service = _service(repo, publisher)

    service.reserve("p1", "order-1", 3)

    assert publisher.low_stock == []  # available 20 -> 17, still above threshold


def test_commit_publishes_when_on_hand_changes(publisher):
    stock = Stock("p1", on_hand=10, reserved=3, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.commit_result = _reservation(status=ReservationStatus.COMMITTED)
    repo.on_hand_after_commit = 7
    service = _service(repo, publisher)

    service.commit("p1", "order-1")

    assert len(publisher.stock_changed) == 1


def test_commit_does_not_publish_on_idempotent_replay(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.commit_result = _reservation(status=ReservationStatus.COMMITTED)
    # on_hand_after_commit left None -> commit_reservation doesn't touch
    # on_hand, modeling the idempotent-no-op (already-terminal) path.
    service = _service(repo, publisher)

    service.commit("p1", "order-1")

    assert publisher.stock_changed == []


def test_release_publishes_stock_changed(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.release_result = _reservation(status=ReservationStatus.RELEASED)
    service = _service(repo, publisher)

    service.release("p1", "order-1")

    assert len(publisher.stock_changed) == 1


def test_adjust_publishes_stock_changed(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    service = _service(repo, publisher)

    service.adjust("p1", "admin-1", 5, "recount")

    assert len(publisher.stock_changed) == 1


def test_receive_publishes_stock_changed(publisher):
    stock = Stock("p1", on_hand=10, reserved=0, low_stock_threshold=5)
    repo = FakeRepository(stock)
    service = _service(repo, publisher)

    service.receive("p1", 5, date(2026, 12, 25), "admin-1", None)

    assert len(publisher.stock_changed) == 1


def test_handle_order_cancelled_publishes_for_each_released_reservation(publisher):
    stock = Stock("p1", on_hand=10, reserved=3, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.release_result = _reservation(status=ReservationStatus.RELEASED)
    service = _service(repo, publisher)

    released = service.handle_order_cancelled("order-1")

    assert len(released) == 1
    assert len(publisher.stock_changed) == 1


def test_run_ttl_sweep_publishes_for_each_released_reservation(publisher):
    stock = Stock("p1", on_hand=10, reserved=3, low_stock_threshold=5)
    repo = FakeRepository(stock)
    repo.release_result = _reservation(status=ReservationStatus.RELEASED)
    service = _service(repo, publisher)

    released = service.run_ttl_sweep()

    assert len(released) == 1
    assert len(publisher.stock_changed) == 1
