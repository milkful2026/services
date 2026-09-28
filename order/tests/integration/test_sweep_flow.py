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
from domain.exceptions import OrderBusyError
from domain.models import DebitResult, Order, OrderStatus
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
        cutoff_hour_ist=20,
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
    assert counts["confirmed"] == 1
    assert repo.get("ord_stuck").status == OrderStatus.CONFIRMED
    assert len(wallet_client.calls) == 1
    events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    assert len(events) == 1


def test_run_once_escalates_past_cutoff_order_without_charging(
    repo, engine, service, wallet_client
):
    # Ticket AC 5 (with D-5's cut-off rule).
    delivery = datetime.now(UTC).astimezone(IST).date()  # cut-off was yesterday 20:00
    _stuck(repo, engine, delivery_date=delivery)
    sweep_handler.run_once(service=_sweep(repo, service))
    assert repo.get("ord_stuck").status == OrderStatus.NEEDS_ATTENTION
    assert wallet_client.calls == []


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
