"""MA-143 FR-4 — the sweep's stuck subscription-order pass, against the
SQLite repository and the conftest fakes."""

from datetime import UTC, date, datetime, timedelta

import pytest

from adapters.order_repository import orders_table
from domain.cutoff import IST
from domain.models import Order, OrderStatus
from domain.sweep_service import SweepService

OWNER = "sweep:test"
MAX_ATTEMPTS = 3


class FakeMetrics:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name, **dimensions):
        self.emitted.append((name, dimensions))

    def names(self):
        return [name for name, _ in self.emitted]


@pytest.fixture
def metrics():
    return FakeMetrics()


@pytest.fixture
def sweep(repo, service, metrics):
    return SweepService(
        repo,
        service,
        metrics,
        owner=OWNER,
        cutoff_hour_ist=20,
        subscription_order_stale_seconds=900,
        max_attempts=MAX_ATTEMPTS,
        lease_seconds=120,
        batch_size=50,
    )


def _today_ist() -> date:
    return datetime.now(UTC).astimezone(IST).date()


def _stuck_order(repo, engine, *, order_id="ord_stuck", delivery_date=None, age_minutes=20,
                 subscription_id="sub-1", **columns):
    """A CREATED subscription order left behind by a crashed materialize."""
    repo.insert_created(
        Order(
            id=order_id,
            user_id="user-1",
            subscription_id=subscription_id,
            product_id="prod-1",
            quantity=2,
            amount_paise=5500,
            delivery_date=delivery_date or _today_ist() + timedelta(days=2),
            status=OrderStatus.CREATED,
        )
    )
    with engine.begin() as conn:
        conn.execute(
            orders_table.update()
            .where(orders_table.c.id == order_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=age_minutes), **columns)
        )


def _events(repo, event_type):
    return [e for e in repo.fetch_unpublished() if e["event_type"] == event_type]


def _now():
    return datetime.now(UTC)


class TestResume:
    def test_debited_confirms_once_with_one_event(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine)
        counts = sweep.sweep_subscription_orders("corr", _now())
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.CONFIRMED
        assert order.claim_owner is None and order.claimed_until is None
        assert wallet_client.calls == [("user-1", "ord_stuck", 5500)]
        assert len(_events(repo, "OrderConfirmed")) == 1
        assert counts["confirmed"] == 1

    def test_insufficient_balance_fails_payment_with_one_event(
        self, sweep, repo, engine, wallet_client
    ):
        wallet_client.result_status = "INSUFFICIENT_BALANCE"
        _stuck_order(repo, engine)
        counts = sweep.sweep_subscription_orders("corr", _now())
        assert repo.get("ord_stuck").status == OrderStatus.PAYMENT_FAILED
        assert len(_events(repo, "OrderPaymentFailed")) == 1
        assert counts["payment_failed"] == 1

    def test_wallet_down_records_attempt_and_releases(self, sweep, repo, engine, wallet_client):
        wallet_client.raise_unavailable = True
        _stuck_order(repo, engine)
        counts = sweep.sweep_subscription_orders("corr", _now())
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.CREATED
        assert order.sweep_attempts == 1
        assert order.last_sweep_error == "WALLET_UNAVAILABLE"
        assert order.claim_owner is None
        assert counts["failed_attempt"] == 1

    def test_last_attempt_escalates_sweep_exhausted(
        self, sweep, repo, engine, wallet_client, metrics
    ):
        wallet_client.raise_unavailable = True
        _stuck_order(repo, engine, sweep_attempts=MAX_ATTEMPTS - 1)
        counts = sweep.sweep_subscription_orders("corr", _now())
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.failure_reason == "SWEEP_EXHAUSTED"
        assert counts["escalated"] == 1
        assert ("sweep.subscription_order.escalated", {"reason": "SWEEP_EXHAUSTED"}) in (
            metrics.emitted
        )
        # Never charged later: an escalated order isn't selected again.
        wallet_client.raise_unavailable = False
        sweep.sweep_subscription_orders("corr", _now())
        assert repo.get("ord_stuck").status == OrderStatus.NEEDS_ATTENTION


class TestCutoff:
    DELIVERY = date(2026, 2, 1)

    def test_one_second_before_cutoff_is_charged(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        now = datetime(2026, 1, 31, 19, 59, 59, tzinfo=IST)
        sweep.sweep_subscription_orders("corr", now)
        assert len(wallet_client.calls) == 1
        assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED

    def test_at_cutoff_escalates_without_any_wallet_call(
        self, sweep, repo, engine, wallet_client, metrics
    ):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        now = datetime(2026, 1, 31, 20, 0, 0, tzinfo=IST)
        counts = sweep.sweep_subscription_orders("corr", now)
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.failure_reason == "CUTOFF_PASSED"
        assert wallet_client.calls == []
        assert repo.fetch_unpublished() == []
        assert counts["escalated"] == 1
        assert ("sweep.subscription_order.escalated", {"reason": "CUTOFF_PASSED"}) in (
            metrics.emitted
        )


class TestSelectionAndClaims:
    def test_fresh_order_is_left_to_sqs(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine, age_minutes=1)
        assert sweep.sweep_subscription_orders("corr", _now()) == {}
        assert wallet_client.calls == []

    def test_lost_claim_skips_without_calls(
        self, sweep, repo, engine, wallet_client, monkeypatch
    ):
        _stuck_order(repo, engine)
        monkeypatch.setattr(repo, "claim_order", lambda *a, **k: False)
        counts = sweep.sweep_subscription_orders("corr", _now())
        assert wallet_client.calls == []
        assert counts == {"found": 1}

    def test_one_failing_record_does_not_stop_the_next(
        self, sweep, repo, engine, service, wallet_client, monkeypatch
    ):
        _stuck_order(repo, engine, order_id="ord_bad", subscription_id="sub-bad", age_minutes=30)
        _stuck_order(repo, engine, order_id="ord_good", subscription_id="sub-good")
        real = service.resume_debit

        def flaky(order, correlation_id):
            if order.id == "ord_bad":
                raise RuntimeError("boom")
            real(order, correlation_id)

        monkeypatch.setattr(service, "resume_debit", flaky)
        sweep.sweep_subscription_orders("corr", _now())
        bad = repo.get("ord_bad")
        assert bad.status == OrderStatus.CREATED
        assert bad.last_sweep_error == "UNEXPECTED:RuntimeError"
        assert bad.claim_owner is None
        assert repo.get("ord_good").status == OrderStatus.CONFIRMED
