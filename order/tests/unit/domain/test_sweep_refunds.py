"""MA-154 FR-5 — the sweep's refund pass: customer cancels whose refund is
still PENDING, against the SQLite repository and the conftest fakes."""

from datetime import UTC, date, datetime, timedelta

import pytest

from adapters.order_repository import orders_table
from domain.exceptions import WalletUnavailableError
from domain.models import RefundState
from domain.sweep_service import SweepService
from handlers.sweep import run_once

OWNER = "sweep:test"
DELIVERY_DATE = date(2026, 2, 1)
_BEFORE_CUTOFF = datetime(2026, 1, 31, 14, 0, tzinfo=UTC)


class FakeMetrics:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name, **dimensions):
        self.emitted.append((name, dimensions))


@pytest.fixture
def metrics():
    return FakeMetrics()


@pytest.fixture
def sweep(repo, service, metrics, wallet_client):
    return SweepService(
        repo,
        service,
        metrics,
        owner=OWNER,
        wallet_client=wallet_client,
        charge_deadline_hour_ist=23,
        subscription_order_stale_seconds=900,
        max_attempts=3,
        lease_seconds=120,
        batch_size=50,
    )


def _pending_refund(service, repo, engine, wallet_client, *, cancelled_ago=timedelta(minutes=2)):
    """A customer-cancelled order whose refund failed (Wallet was down)."""
    service.materialize(
        subscription_id="sub-1",
        user_id="user-1",
        product_id="prod-1",
        quantity=2,
        delivery_date=DELIVERY_DATE,
        correlation_id="c",
    )
    order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
    wallet_client.refund_exception = WalletUnavailableError("down")
    service.cancel(order.id, "user-1", None, _BEFORE_CUTOFF, "c")
    wallet_client.refund_exception = None
    with engine.begin() as conn:
        conn.execute(
            orders_table.update()
            .where(orders_table.c.id == order.id)
            .values(cancelled_at=datetime.now(UTC) - cancelled_ago)
        )
    assert repo.get(order.id).refund_state == RefundState.PENDING
    return order.id


def _now():
    return datetime.now(UTC)


def test_stale_pending_refund_is_refunded(sweep, service, repo, engine, wallet_client):
    order_id = _pending_refund(service, repo, engine, wallet_client)
    counts = sweep.finish_pending_refunds("corr", _now())
    assert counts == {"found": 1, "refunded": 1}
    order = repo.get(order_id)
    assert order.refund_state == RefundState.REFUNDED
    assert order.claim_owner is None
    assert len(wallet_client.refund_calls) == 2  # the request's try, then the sweep's


def test_fresh_pending_refund_is_left_to_the_request(sweep, service, repo, engine, wallet_client):
    order_id = _pending_refund(
        service, repo, engine, wallet_client, cancelled_ago=timedelta(seconds=30)
    )
    assert sweep.finish_pending_refunds("corr", _now()) == {}
    assert repo.get(order_id).refund_state == RefundState.PENDING


def test_order_leased_by_another_worker_is_skipped(sweep, service, repo, engine, wallet_client):
    order_id = _pending_refund(service, repo, engine, wallet_client)
    assert repo.claim_pending_refund(order_id, "sweep:other", 120)
    assert sweep.finish_pending_refunds("corr", _now()) == {}
    assert repo.get(order_id).refund_state == RefundState.PENDING


def test_wallet_still_down_stays_pending_and_releases(
    sweep, service, repo, engine, wallet_client
):
    order_id = _pending_refund(service, repo, engine, wallet_client)
    wallet_client.refund_exception = WalletUnavailableError("still down")
    assert sweep.finish_pending_refunds("corr", _now()) == {"found": 1, "still_pending": 1}
    order = repo.get(order_id)
    assert order.refund_state == RefundState.PENDING
    assert order.claim_owner is None  # retried next run


def test_pending_age_metric_reports_the_oldest(sweep, service, repo, engine, wallet_client,
                                               metrics):
    _pending_refund(service, repo, engine, wallet_client, cancelled_ago=timedelta(minutes=20))
    wallet_client.refund_exception = WalletUnavailableError("still down")
    sweep.finish_pending_refunds("corr", _now())
    [(_, dims)] = [m for m in metrics.emitted if m[0] == "order.refund.pending_age_seconds"]
    assert 1190 <= dims["value"] <= 1260


def test_pending_age_metric_is_zero_with_nothing_pending(sweep, metrics):
    sweep.finish_pending_refunds("corr", _now())
    assert ("order.refund.pending_age_seconds", {"value": 0}) in metrics.emitted


def test_run_once_includes_the_refund_pass(sweep, service, repo, engine, wallet_client):
    _pending_refund(service, repo, engine, wallet_client)
    counts = run_once(service=sweep)
    assert counts["refund.refunded"] == 1


def test_mark_refund_state_is_a_noop_once_resolved(service, repo, engine, wallet_client):
    order_id = _pending_refund(service, repo, engine, wallet_client)
    assert repo.mark_refund_state(order_id, RefundState.REFUNDED, refunded_at=_now())
    assert not repo.mark_refund_state(order_id, RefundState.NOT_REQUIRED)
    assert repo.get(order_id).refund_state == RefundState.REFUNDED


def test_cancel_by_customer_writes_nothing_unless_confirmed(service, repo, engine, wallet_client):
    order_id = _pending_refund(service, repo, engine, wallet_client)  # already CANCELLED
    won = repo.cancel_by_customer(
        order_id,
        reason=None,
        now=_now(),
        refund_state=RefundState.PENDING,
        outbox_payload={"eventId": "dup"},
    )
    assert won is False
    cancelled = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderCancelled"]
    assert len(cancelled) == 1
