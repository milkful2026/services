"""MA-136 — the checkout tables' DB-level guarantees (SQLite, same
constraints as 0002_checkout.sql)."""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from adapters.order_repository import orders_table
from domain.checkout_models import (
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CheckoutStep,
    SubscriptionLineResult,
)
from domain.exceptions import CheckoutInProgressError
from domain.models import Order, OrderItem, OrderSource, OrderStatus


def _checkout(checkout_id="chk_1", key="key-1", order_id=None):
    return Checkout(
        id=checkout_id,
        user_id="user-1",
        idempotency_key=key,
        cart_version=3,
        status=CheckoutStatus.IN_PROGRESS,
        step=CheckoutStep.STARTED,
        lines=[
            CheckoutLine("li-1", "buffalo-milk", 1, "ONE_TIME"),
            CheckoutLine("li-2", "cow-milk", 2, "DAILY", date(2026, 9, 27), "slot-am"),
        ],
        pay_now_paise=5500,
        delivery_date=date(2026, 9, 26),
        order_id=order_id,
    )


def _order(order_id="ord_1", checkout_id="chk_1"):
    return Order(
        id=order_id,
        user_id="user-1",
        subscription_id=None,
        product_id=None,
        quantity=None,
        amount_paise=5500,
        delivery_date=date(2026, 9, 26),
        status=OrderStatus.CREATED,
        source=OrderSource.CHECKOUT,
        checkout_id=checkout_id,
        items=[OrderItem("buffalo-milk", 1), OrderItem("paneer", 2)],
    )


def test_start_checkout_round_trips_lines_order_and_items(repo):
    repo.start_checkout(_checkout(order_id="ord_1"), _order())

    stored = repo.get_checkout("user-1", "key-1")
    assert stored.lines[1].start_date == date(2026, 9, 27)
    assert stored.lines[1].slot_id == "slot-am"
    assert stored.order_id == "ord_1"

    order = repo.get("ord_1")
    assert order.source == OrderSource.CHECKOUT
    assert order.checkout_id == "chk_1"
    assert order.items == [OrderItem("buffalo-milk", 1), OrderItem("paneer", 2)]
    assert repo.list_for_user("user-1", None, 10, None).items[0].items == order.items


def test_same_key_race_returns_the_existing_checkout(repo):
    repo.start_checkout(_checkout("chk_1"), None)

    winner = repo.start_checkout(_checkout("chk_2"), None)

    assert winner.id == "chk_1"


def test_only_one_live_checkout_per_user(repo):
    repo.start_checkout(_checkout("chk_1", key="key-1"), None)

    with pytest.raises(CheckoutInProgressError):
        repo.start_checkout(_checkout("chk_2", key="key-2"), None)


def test_a_finished_checkout_frees_the_live_slot(repo):
    repo.start_checkout(_checkout("chk_1", key="key-1"), None)
    repo.update_checkout("chk_1", status=CheckoutStatus.COMPLETED, result={"ok": True})

    repo.start_checkout(_checkout("chk_2", key="key-2"), None)

    assert repo.get_live_checkout("user-1").id == "chk_2"


def test_update_checkout_persists_step_results_and_result(repo):
    repo.start_checkout(_checkout(), None)
    results = [SubscriptionLineResult("li-2", "cow-milk", "CREATED", "sub_1", "2026-09-27")]

    repo.update_checkout(
        "chk_1", step=CheckoutStep.PAID, subscription_results=results, result={"x": 1}
    )

    stored = repo.get_checkout("user-1", "key-1")
    assert stored.step == CheckoutStep.PAID
    assert stored.subscription_results == results
    assert stored.result == {"x": 1}


def test_checkout_order_must_not_carry_a_subscription(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            orders_table.insert().values(
                id="ord_bad", user_id="user-1", subscription_id="sub-1", product_id=None,
                quantity=None, amount_paise=100, delivery_date=date(2026, 9, 26),
                status="CREATED", source="CHECKOUT", checkout_id="chk_x",
            )
        )


def test_subscription_order_still_requires_its_product(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            orders_table.insert().values(
                id="ord_bad", user_id="user-1", subscription_id="sub-1", product_id=None,
                quantity=None, amount_paise=100, delivery_date=date(2026, 9, 26),
                status="CREATED", source="SUBSCRIPTION",
            )
        )


def test_one_order_per_checkout_is_enforced_by_the_db(engine):
    # start_checkout always stamps the order with its own checkout's id, so
    # the repository can't produce a duplicate — this pins the DB-level
    # guard (orders.checkout_id UNIQUE) behind that.
    row = dict(
        user_id="user-1", subscription_id=None, product_id=None, quantity=None,
        amount_paise=100, delivery_date=date(2026, 9, 26), status="CREATED",
        source="CHECKOUT", checkout_id="chk_1",
    )
    with engine.begin() as conn:
        conn.execute(orders_table.insert().values(id="ord_1", **row))

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(orders_table.insert().values(id="ord_2", **row))
