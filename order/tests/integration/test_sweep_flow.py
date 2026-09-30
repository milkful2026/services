"""MA-143 end-to-end: a stuck subscription order recovered through the
sweep loop's run_once, the SQS-vs-sweep race, loop robustness and the
/healthz contract."""

import importlib
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from adapters.order_repository import orders_table
from domain.cutoff import IST
from domain.exceptions import DebitVoidedError, OrderBusyError
from domain.models import ChargeState, DebitLookup, DebitResult, Order, OrderStatus, Voided
from domain.order_service import OrderService
from domain.sweep_service import SweepService
from handlers import sweep as sweep_handler
from handlers.app import app
from handlers.health import sweep_health


class _Metrics:
    def emit(self, name, **dimensions):
        pass


def _sweep(repo, order_service):
    return SweepService(
        repo,
        order_service,
        _Metrics(),
        owner="sweep:test",
        wallet_client=order_service._wallet_client,
        charge_deadline_hour_ist=23,
        subscription_order_stale_seconds=900,
        max_attempts=3,
        lease_seconds=120,
        batch_size=50,
    )


def _stuck(repo, engine, *, delivery_date=None, order_id="ord_stuck"):
    delivery = delivery_date or datetime.now(UTC).astimezone(IST).date() + timedelta(days=2)
    repo.insert_created(
        Order(
            id=order_id,
            user_id="user-1",
            subscription_id="sub-1",
            product_id="prod-1",
            quantity=2,
            amount_paise=5500,
            delivery_date=delivery,
            status=OrderStatus.CREATED,
        )
    )
    with engine.begin() as conn:
        conn.execute(
            orders_table.update()
            .where(orders_table.c.id == order_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=16))
        )
    return delivery


def test_run_once_confirms_stuck_order_with_one_debit_and_one_event(
    repo, engine, service, wallet_client
):
    # Ticket AC 4.
    _stuck(repo, engine)
    counts = sweep_handler.run_once(service=_sweep(repo, service))
    assert counts["subscription_order.confirmed"] == 1
    assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED
    assert len(wallet_client.calls) == 1
    events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    assert len(events) == 1


def test_run_once_closes_past_deadline_order_without_charging(
    repo, engine, service, wallet_client
):
    # Ticket AC 5 (with the amended D-5 charge deadline).
    delivery = datetime.now(UTC).astimezone(IST).date()  # deadline was yesterday 23:00
    _stuck(repo, engine, delivery_date=delivery)
    sweep_handler.run_once(service=_sweep(repo, service))
    order = repo.get("ord_stuck")
    assert order.status == OrderStatus.NEEDS_ATTENTION
    assert order.charge_state == ChargeState.NOT_CHARGED
    assert wallet_client.void_calls == ["ord_stuck"]
    assert wallet_client.calls == []


def test_run_once_settles_an_escalated_order(repo, engine, service, wallet_client):
    _stuck(repo, engine)
    with engine.begin() as conn:
        conn.execute(
            orders_table.update().values(status="NEEDS_ATTENTION", charge_state="UNKNOWN")
        )
    counts = sweep_handler.run_once(service=_sweep(repo, service))
    assert counts["settle.settled_not_charged"] == 1
    assert repo.get("ord_stuck").charge_state == ChargeState.NOT_CHARGED


class _BlockingWallet:
    """Debits succeed, but the first call waits until released, so a
    second worker can try the same order while it is in flight."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def debit(self, user_id, order_id, amount_paise, correlation_id):
        self.calls.append(order_id)
        self.entered.set()
        assert self.release.wait(5)
        return DebitResult(status="DEBITED", balance_after_paise=0)


def test_sqs_redelivery_racing_the_sweep_debits_once(repo, engine, user_client, pricing_client):
    # Ticket AC 6 (subscription half).
    wallet = _BlockingWallet()
    order_service = OrderService(repo, user_client, pricing_client, wallet)
    delivery = _stuck(repo, engine)
    sweep = _sweep(repo, order_service)

    worker = threading.Thread(
        target=sweep.sweep_subscription_orders, args=("corr", datetime.now(UTC))
    )
    worker.start()
    assert wallet.entered.wait(5)
    with pytest.raises(OrderBusyError):
        order_service.materialize(
            subscription_id="sub-1",
            user_id="user-1",
            product_id="prod-1",
            quantity=2,
            delivery_date=delivery,
            correlation_id="corr-sqs",
            claim_owner="sqs:m1",
        )
    wallet.release.set()
    worker.join(5)

    assert wallet.calls == ["ord_stuck"]
    assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED
    events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    assert len(events) == 1


def test_run_forever_survives_a_failing_run(monkeypatch):
    stop = threading.Event()
    calls = []

    def flaky_run_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db down")
        stop.set()

    monkeypatch.setenv("ORDER_SWEEP_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(sweep_handler, "run_once", flaky_run_once)
    sweep_handler.run_forever(stop)
    assert len(calls) == 2


def test_dead_sweep_thread_fails_healthz(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_LOCAL_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("ORDER_SWEEP_ENABLED", "true")
    main = importlib.import_module("main")

    def crash(*_):
        raise RuntimeError("thread died")

    monkeypatch.setattr(main.sweep, "run_forever", crash)
    monkeypatch.setattr(sweep_health, "alive", True)
    with pytest.raises(RuntimeError):
        main._run_sweep()
    assert sweep_health.alive is False

    r = TestClient(app).get("/healthz")
    assert r.status_code == 503
    assert r.json()["reason"] == "order_sweep stopped"


def test_disabled_sweep_does_not_affect_healthz(monkeypatch):
    monkeypatch.setattr(sweep_health, "alive", False)
    # conftest sets ORDER_SWEEP_ENABLED=false
    assert TestClient(app).get("/healthz").status_code == 200


class _LockingWallet:
    """MA-142's semantics in miniature: a debit and a void for the same
    user serialize on one lock, and at most one of {debit, void} ever exists
    per order. `pause_debit` holds the first debit inside the lock, like a
    Wallet transaction still running after its client timed out."""

    def __init__(self, pause_debit=False):
        self._lock = threading.Lock()
        self.debited: dict[str, DebitLookup] = {}
        self.voided: dict[str, datetime] = {}
        self.debit_calls = []
        self.pause_debit = pause_debit
        self.entered = threading.Event()
        self.release = threading.Event()

    def debit(self, user_id, order_id, amount_paise, correlation_id):
        self.debit_calls.append(order_id)
        with self._lock:
            if self.pause_debit:
                self.pause_debit = False
                self.entered.set()
                assert self.release.wait(5)
            if order_id in self.debited:
                return DebitResult(status="DEBITED", balance_after_paise=0)
            if order_id in self.voided:
                raise DebitVoidedError("voided", {"orderId": order_id})
            self.debited[order_id] = DebitLookup(amount_paise, 0, datetime.now(UTC))
            return DebitResult(status="DEBITED", balance_after_paise=0)

    def void_debit(self, user_id, order_id):
        with self._lock:
            if order_id in self.debited:
                return self.debited[order_id]
            self.voided.setdefault(order_id, datetime.now(UTC))
            return Voided(self.voided[order_id])


def _past_deadline_stuck(repo, engine):
    _stuck(repo, engine, delivery_date=datetime.now(UTC).astimezone(IST).date())


def _assert_exactly_one_outcome(repo, wallet):
    order = repo.get("ord_stuck")
    confirmed = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    if "ord_stuck" in wallet.debited:
        assert "ord_stuck" not in wallet.voided
        assert order.status == OrderStatus.CONFIRMED
        assert len(confirmed) == 1
    else:
        assert "ord_stuck" in wallet.voided
        assert order.status == OrderStatus.NEEDS_ATTENTION
        assert order.charge_state == ChargeState.NOT_CHARGED
        assert confirmed == []
    return order.status


def test_in_flight_debit_that_commits_first_is_confirmed_not_closed(
    repo, engine, user_client, pricing_client
):
    # The earlier debit holds the wallet lock; the sweep's void waits for it,
    # sees the debit, and confirms the order instead of closing it.
    wallet = _LockingWallet(pause_debit=True)
    order_service = OrderService(repo, user_client, pricing_client, wallet)
    _past_deadline_stuck(repo, engine)
    late = threading.Thread(target=wallet.debit, args=("user-1", "ord_stuck", 5500, "c"))
    late.start()
    assert wallet.entered.wait(5)
    worker = threading.Thread(
        target=_sweep(repo, order_service).sweep_subscription_orders,
        args=("corr", datetime.now(UTC)),
    )
    worker.start()
    wallet.release.set()
    late.join(5)
    worker.join(5)
    assert _assert_exactly_one_outcome(repo, wallet) == OrderStatus.CONFIRMED


def test_debit_arriving_after_the_void_is_refused(repo, engine, user_client, pricing_client):
    wallet = _LockingWallet()
    order_service = OrderService(repo, user_client, pricing_client, wallet)
    _past_deadline_stuck(repo, engine)
    _sweep(repo, order_service).sweep_subscription_orders("corr", datetime.now(UTC))
    with pytest.raises(DebitVoidedError):
        wallet.debit("user-1", "ord_stuck", 5500, "late")
    assert _assert_exactly_one_outcome(repo, wallet) == OrderStatus.NEEDS_ATTENTION
