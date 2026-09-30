"""MA-143 FR-2/FR-3 — lease and attempt bookkeeping on the SQLite
repository (the same SQL runs on Postgres; see the plan's migration check)."""

from datetime import UTC, date, datetime, timedelta

from adapters.order_repository import orders_table
from domain.models import ChargeState, Order, OrderStatus


def _order(repo, engine, order_id="ord_1", *, source_checkout=False, age_minutes=20, **columns):
    repo.insert_created(
        Order(
            id=order_id,
            user_id="user-1",
            subscription_id=f"sub-{order_id}",
            product_id="prod-1",
            quantity=1,
            amount_paise=1000,
            delivery_date=date(2026, 2, 1),
            status=OrderStatus.CREATED,
        )
    )
    values = {"created_at": datetime.now(UTC) - timedelta(minutes=age_minutes), **columns}
    if source_checkout:
        values.update(
            source="CHECKOUT", checkout_id=f"chk-{order_id}", subscription_id=None,
        )
    with engine.begin() as conn:
        conn.execute(orders_table.update().where(orders_table.c.id == order_id).values(**values))


def test_claim_is_exclusive_until_it_expires(repo, engine):
    _order(repo, engine)
    assert repo.claim_order("ord_1", "a", 120) is True
    assert repo.claim_order("ord_1", "b", 120) is False
    # Simulate the holder dying: its lease runs out.
    with engine.begin() as conn:
        conn.execute(
            orders_table.update()
            .where(orders_table.c.id == "ord_1")
            .values(claimed_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert repo.claim_order("ord_1", "b", 120) is True
    assert repo.get("ord_1").claim_owner == "b"


def test_renew_and_release_by_non_owner_change_nothing(repo, engine):
    _order(repo, engine)
    repo.claim_order("ord_1", "a", 120)
    before = repo.get("ord_1").claimed_until
    assert repo.renew_order("ord_1", "b", 600) is False
    repo.release_order("ord_1", "b")
    after = repo.get("ord_1")
    assert after.claim_owner == "a"
    assert after.claimed_until == before
    assert repo.renew_order("ord_1", "a", 600) is True
    repo.release_order("ord_1", "a")
    assert repo.get("ord_1").claim_owner is None


def test_claim_requires_created(repo, engine):
    _order(repo, engine, status=OrderStatus.CONFIRMED.value)
    assert repo.claim_order("ord_1", "a", 120) is False


def test_stale_selection_ignores_fresh_terminal_checkout_and_leased(repo, engine):
    _order(repo, engine, "ord_stale")
    _order(repo, engine, "ord_fresh", age_minutes=1)
    _order(repo, engine, "ord_done", status=OrderStatus.CONFIRMED.value)
    _order(repo, engine, "ord_checkout", source_checkout=True)
    _order(
        repo, engine, "ord_leased",
        claim_owner="x", claimed_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert repo.list_stale_subscription_orders(900, 50) == ["ord_stale"]


def test_failure_counts_then_escalates_exactly_at_max(repo, engine):
    _order(repo, engine)
    for _ in range(2):
        repo.claim_order("ord_1", "a", 120)
        assert repo.record_order_sweep_failure("ord_1", "a", "WALLET_UNAVAILABLE", 3) is False
    repo.claim_order("ord_1", "a", 120)
    assert repo.record_order_sweep_failure("ord_1", "a", "WALLET_UNAVAILABLE", 3) is True
    order = repo.get("ord_1")
    assert order.sweep_attempts == 3
    assert order.status == OrderStatus.NEEDS_ATTENTION
    assert order.failure_reason == "SWEEP_EXHAUSTED"
    assert order.claim_owner is None


def test_terminal_transition_clears_the_lease(repo, engine):
    _order(repo, engine)
    repo.claim_order("ord_1", "a", 120)
    repo.mark_confirmed("ord_1", datetime.now(UTC), "OrderConfirmed", {"orderId": "ord_1"})
    order = repo.get("ord_1")
    assert order.status == OrderStatus.CONFIRMED
    assert order.claim_owner is None and order.claimed_until is None


def test_escalate_needs_the_lease_holder(repo, engine):
    _order(repo, engine)
    repo.claim_order("ord_1", "a", 120)
    assert repo.close_order("ord_1", "b", "CUTOFF_PASSED") is False
    assert repo.close_order("ord_1", "a", "CUTOFF_PASSED") is True
    order = repo.get("ord_1")
    assert order.status == OrderStatus.NEEDS_ATTENTION
    assert order.charge_state == ChargeState.NOT_CHARGED
    assert order.claim_owner is None


def test_exhaustion_marks_the_charge_unknown(repo, engine):
    _order(repo, engine, sweep_attempts=2)
    repo.claim_order("ord_1", "a", 120)
    assert repo.record_order_sweep_failure("ord_1", "a", "WALLET_UNAVAILABLE", 3) is True
    assert repo.get("ord_1").charge_state == ChargeState.UNKNOWN


def test_settle_selection_only_returns_unknown_charges(repo, engine):
    _order(repo, engine, "ord_unknown", status="NEEDS_ATTENTION", charge_state="UNKNOWN")
    _order(repo, engine, "ord_settled", status="NEEDS_ATTENTION", charge_state="NOT_CHARGED")
    _order(repo, engine, "ord_created")
    _order(repo, engine, "ord_leased", status="NEEDS_ATTENTION", charge_state="UNKNOWN")
    assert repo.claim_unknown_charge_order("ord_leased", "x", 120)
    assert repo.list_unknown_charge_orders(10) == ["ord_unknown"]


def test_settle_changes_only_the_charge_state_and_releases(repo, engine):
    _order(repo, engine, status="NEEDS_ATTENTION", charge_state="UNKNOWN")
    assert repo.claim_unknown_charge_order("ord_1", "a", 120)
    assert repo.settle_charge_state("ord_1", "b", ChargeState.CHARGED) is False
    assert repo.settle_charge_state("ord_1", "a", ChargeState.CHARGED) is True
    order = repo.get("ord_1")
    assert (order.status, order.charge_state) == (OrderStatus.NEEDS_ATTENTION, ChargeState.CHARGED)
    assert order.claim_owner is None


def test_carried_keys_carry_idempotently_and_forget(repo):
    repo.update_checkout("chk-a", user_id="user-1", carry_keys={"li-1": "checkout:chk-a:li-1"})
    # A second carry for the same line keeps the first key.
    repo.update_checkout("chk-b", user_id="user-1", carry_keys={"li-1": "checkout:chk-b:li-1"})
    assert repo.get_carried_keys("user-1") == {"li-1": ("checkout:chk-a:li-1", "chk-a")}
    assert repo.get_carried_keys("user-2") == {}
    repo.update_checkout("chk-b", user_id="user-1", forget_line_ids=["li-1"])
    assert repo.get_carried_keys("user-1") == {}
