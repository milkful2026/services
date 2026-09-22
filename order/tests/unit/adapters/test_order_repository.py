from datetime import UTC, date, datetime

from domain.models import Order, OrderStatus

DELIVERY_DATE = date(2026, 2, 1)


def _created_order(**overrides) -> Order:
    defaults = dict(
        id="ord-1",
        user_id="user-1",
        subscription_id="sub-1",
        product_id="prod-1",
        quantity=1,
        amount_paise=1000,
        delivery_date=DELIVERY_DATE,
        status=OrderStatus.CREATED,
    )
    defaults.update(overrides)
    return Order(**defaults)


class TestConcurrentRedeliveryGuard:
    def test_mark_confirmed_is_noop_if_order_already_confirmed(self, repo):
        # Regression: two concurrent redeliveries can both resume the
        # same CREATED order and both call mark_confirmed — only the
        # first (still CREATED) transition may publish an OrderConfirmed;
        # the second must be a silent no-op, not a duplicate outbox row
        # with a fresh eventId.
        repo.insert_created(_created_order())
        repo.mark_confirmed(
            "ord-1", datetime.now(UTC), "OrderConfirmed", {"eventId": "evt-1", "n": 1}
        )
        repo.mark_confirmed(
            "ord-1", datetime.now(UTC), "OrderConfirmed", {"eventId": "evt-2", "n": 2}
        )
        events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
        assert len(events) == 1
        assert events[0]["payload"]["eventId"] == "evt-1"

    def test_mark_payment_failed_is_noop_if_order_already_resolved(self, repo):
        repo.insert_created(_created_order())
        repo.mark_payment_failed(
            "ord-1", "INSUFFICIENT_BALANCE", "OrderPaymentFailed", {"eventId": "evt-1"}
        )
        repo.mark_payment_failed(
            "ord-1", "INSUFFICIENT_BALANCE", "OrderPaymentFailed", {"eventId": "evt-2"}
        )
        events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"]
        assert len(events) == 1
        assert events[0]["payload"]["eventId"] == "evt-1"

    def test_mark_confirmed_is_noop_if_already_payment_failed(self, repo):
        # A concurrent racer resolved it the *other* way first — must
        # not flip a terminal PAYMENT_FAILED order to CONFIRMED.
        repo.insert_created(_created_order())
        repo.mark_payment_failed(
            "ord-1", "INSUFFICIENT_BALANCE", "OrderPaymentFailed", {"eventId": "evt-1"}
        )
        repo.mark_confirmed(
            "ord-1", datetime.now(UTC), "OrderConfirmed", {"eventId": "evt-2"}
        )
        order = repo.get("ord-1")
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"] == []


class TestInsertPaymentFailedRace:
    def test_race_returns_winner_without_a_second_outbox_row(self, repo):
        # Regression: insert_created's IntegrityError recovery used to
        # unconditionally return the pre-existing row, and callers always
        # proceeded to call mark_confirmed/mark_payment_failed again on
        # it — for the pre-pricing paths, insert_payment_failed replaces
        # that two-step sequence with one atomic insert, so the race
        # itself must be resolved with no second outbox write at all.
        winner = Order(
            id="ord-winner",
            user_id="user-1",
            subscription_id="sub-1",
            product_id="prod-1",
            quantity=1,
            amount_paise=0,
            delivery_date=DELIVERY_DATE,
            status=OrderStatus.PAYMENT_FAILED,
            failure_reason="DELIVERY_ADDRESS_UNKNOWN",
        )
        repo.insert_payment_failed(
            winner, "OrderPaymentFailed", {"eventId": "evt-winner", "orderId": "ord-winner"}
        )

        loser = Order(
            id="ord-loser",
            user_id="user-1",
            subscription_id="sub-1",
            product_id="prod-1",
            quantity=1,
            amount_paise=0,
            delivery_date=DELIVERY_DATE,
            status=OrderStatus.PAYMENT_FAILED,
            failure_reason="DELIVERY_ADDRESS_UNKNOWN",
        )
        result = repo.insert_payment_failed(
            loser, "OrderPaymentFailed", {"eventId": "evt-loser", "orderId": "ord-loser"}
        )

        assert result.id == "ord-winner"
        events = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"]
        assert len(events) == 1
        assert events[0]["payload"]["eventId"] == "evt-winner"
