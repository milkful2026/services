"""MA-143 FR-4 — the sweep's stuck subscription-order pass, against the
SQLite repository and the conftest fakes."""

from datetime import UTC, date, datetime, timedelta

import pytest

from adapters.order_repository import orders_table
from config.env import Settings
from domain.cutoff import IST
from domain.models import ChargeState, DebitLookup, Order, OrderStatus
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
def sweep(repo, service, metrics, wallet_client):
    return SweepService(
        repo,
        service,
        metrics,
        owner=OWNER,
        wallet_client=wallet_client,
        charge_deadline_hour_ist=23,
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


class TestChargeDeadline:
    """D-5 (amended): subscription orders may be charged until 23:00 IST the
    day before delivery; past it they are closed only through the void."""

    DELIVERY = date(2026, 2, 1)
    DEADLINE = datetime(2026, 1, 31, 23, 0, 0, tzinfo=IST)

    def test_order_created_by_the_daily_run_at_the_cutoff_is_charged(
        self, sweep, repo, engine, wallet_client
    ):
        # Regression for the PR #30 finding: the Daily Run creates every
        # subscription order at 20:00, when the checkout cut-off has passed.
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        now = datetime(2026, 1, 31, 20, 15, 0, tzinfo=IST)  # stale after 15 min
        counts = sweep.sweep_subscription_orders("corr", now)
        assert wallet_client.void_calls == []
        assert len(wallet_client.calls) == 1
        assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED
        assert counts["confirmed"] == 1

    def test_one_second_before_deadline_is_charged(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        sweep.sweep_subscription_orders("corr", self.DEADLINE - timedelta(seconds=1))
        assert len(wallet_client.calls) == 1
        assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED

    def test_at_deadline_voids_then_closes_without_any_debit(
        self, sweep, repo, engine, wallet_client, metrics
    ):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        counts = sweep.sweep_subscription_orders("corr", self.DEADLINE)
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.failure_reason == "CUTOFF_PASSED"
        assert order.charge_state == ChargeState.NOT_CHARGED
        assert order.claim_owner is None
        assert wallet_client.void_calls == ["ord_stuck"]
        assert wallet_client.calls == []
        assert repo.fetch_unpublished() == []
        assert counts["escalated"] == 1
        assert ("sweep.subscription_order.escalated", {"reason": "CUTOFF_PASSED"}) in (
            metrics.emitted
        )

    def test_past_deadline_already_debited_is_confirmed_not_closed(
        self, sweep, repo, engine, wallet_client, metrics
    ):
        # Crash after the debit, before mark_confirmed, found after the deadline.
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        wallet_client.debited["ord_stuck"] = DebitLookup(5500, 94500, datetime.now(UTC))
        counts = sweep.sweep_subscription_orders("corr", self.DEADLINE)
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.CONFIRMED
        assert order.charge_state is None
        assert len(wallet_client.calls) == 1  # the replay, not a second charge
        assert len(_events(repo, "OrderConfirmed")) == 1
        assert counts["confirmed"] == 1
        assert ("sweep.subscription_order.charged_after_cutoff", {}) in metrics.emitted

    def test_past_deadline_void_unavailable_never_closes(
        self, sweep, repo, engine, wallet_client
    ):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        wallet_client.raise_void_unavailable = True
        counts = sweep.sweep_subscription_orders("corr", self.DEADLINE)
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.CREATED
        assert order.sweep_attempts == 1
        assert order.claim_owner is None
        assert wallet_client.calls == []
        assert counts["failed_attempt"] == 1

    def test_crash_after_void_is_closed_by_the_next_run(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        wallet_client.voided["ord_stuck"] = datetime.now(UTC)  # voided, status never written
        sweep.sweep_subscription_orders("corr", self.DEADLINE)
        assert repo.get("ord_stuck").charge_state == ChargeState.NOT_CHARGED

    def test_debit_refused_voided_is_skipped_without_writing(
        self, sweep, repo, engine, wallet_client
    ):
        # Before the deadline, but another worker already voided it.
        _stuck_order(repo, engine, delivery_date=self.DELIVERY)
        wallet_client.voided["ord_stuck"] = datetime.now(UTC)
        counts = sweep.sweep_subscription_orders("corr", self.DEADLINE - timedelta(hours=1))
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.CREATED
        assert order.sweep_attempts == 0
        assert order.claim_owner is None
        assert repo.fetch_unpublished() == []
        assert counts["skipped"] == 1

    def test_exhaustion_leaves_the_charge_unknown(self, sweep, repo, engine, wallet_client):
        _stuck_order(repo, engine, delivery_date=self.DELIVERY, sweep_attempts=MAX_ATTEMPTS - 1)
        wallet_client.raise_void_unavailable = True
        sweep.sweep_subscription_orders("corr", self.DEADLINE)
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.failure_reason == "SWEEP_EXHAUSTED"
        assert order.charge_state == ChargeState.UNKNOWN


class TestSettlePass:
    def _escalated_unknown(self, repo, engine):
        _stuck_order(repo, engine, status="NEEDS_ATTENTION", failure_reason="SWEEP_EXHAUSTED",
                     charge_state="UNKNOWN")

    def test_voided_settles_not_charged(self, sweep, repo, engine, wallet_client, metrics):
        self._escalated_unknown(repo, engine)
        counts = sweep.settle_unknown_charges("corr", _now())
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.charge_state == ChargeState.NOT_CHARGED
        assert order.claim_owner is None
        assert counts["settled_not_charged"] == 1
        assert ("sweep.settle.settled_not_charged", {}) in metrics.emitted
        assert wallet_client.calls == []  # never charges

    def test_debited_settles_charged_and_alarms(
        self, sweep, repo, engine, wallet_client, metrics, caplog
    ):
        self._escalated_unknown(repo, engine)
        wallet_client.debited["ord_stuck"] = DebitLookup(5500, 94500, datetime.now(UTC))
        with caplog.at_level("ERROR"):
            counts = sweep.settle_unknown_charges("corr", _now())
        order = repo.get("ord_stuck")
        assert order.status == OrderStatus.NEEDS_ATTENTION  # status never changes here
        assert order.charge_state == ChargeState.CHARGED
        assert counts["escalated_charged"] == 1
        assert ("sweep.settle.escalated_charged", {}) in metrics.emitted
        [record] = [r for r in caplog.records if "was charged" in r.getMessage()]
        assert (record.orderId, record.userId, record.amountPaise) == (
            "ord_stuck", "user-1", 5500
        )
        assert record.debitedAt

    def test_unavailable_is_retried_next_run_without_a_budget(
        self, sweep, repo, engine, wallet_client
    ):
        self._escalated_unknown(repo, engine)
        wallet_client.raise_void_unavailable = True
        for _ in range(MAX_ATTEMPTS + 1):
            assert sweep.settle_unknown_charges("corr", _now())["unavailable"] == 1
        order = repo.get("ord_stuck")
        assert order.charge_state == ChargeState.UNKNOWN
        assert order.claim_owner is None
        assert order.sweep_attempts == 0
        wallet_client.raise_void_unavailable = False
        sweep.settle_unknown_charges("corr", _now())
        assert repo.get("ord_stuck").charge_state == ChargeState.NOT_CHARGED

    def test_settled_orders_are_not_selected_again(self, sweep, repo, engine, wallet_client):
        self._escalated_unknown(repo, engine)
        sweep.settle_unknown_charges("corr", _now())
        assert sweep.settle_unknown_charges("corr", _now()) == {}
        assert wallet_client.void_calls == ["ord_stuck"]


class TestDeadlineConfig:
    def test_default_deadline_is_23(self, settings):
        assert settings.subscription_charge_deadline_hour_ist == 23

    @pytest.mark.parametrize("hour", ["20", "19", "24"])
    def test_startup_rejects_a_deadline_not_after_the_cutoff(self, monkeypatch, hour):
        monkeypatch.setenv("ORDER_SUBSCRIPTION_CHARGE_DEADLINE_HOUR_IST", hour)
        with pytest.raises(ValueError):
            Settings()


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
