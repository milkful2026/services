from datetime import UTC, date, datetime, timedelta

import jsonschema
import pytest
from shared.events import load_schema

from adapters.order_repository import orders_table
from domain.exceptions import (
    AddressLookupUnavailableError,
    CutoffPassedError,
    DebitNotFoundError,
    OrderNotCancellableError,
    OrderNotFoundError,
    PricingUnavailableError,
    RefundExceedsDebitError,
    ValidationError,
    WalletUnavailableError,
)
from domain.models import CancelReason, OrderStatus, RefundState

DELIVERY_DATE = date(2026, 2, 1)


def _materialize(service, **overrides):
    kwargs = dict(
        subscription_id="sub-1",
        user_id="user-1",
        product_id="prod-1",
        quantity=2,
        delivery_date=DELIVERY_DATE,
        correlation_id="corr-1",
    )
    kwargs.update(overrides)
    service.materialize(**kwargs)


class TestFreshMaterialization:
    def test_debited_confirms_order_and_enqueues_orderconfirmed(
        self, service, repo, wallet_client
    ):
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.CONFIRMED
        assert order.confirmed_at is not None
        assert wallet_client.calls == [("user-1", order.id, order.amount_paise)]

        unpub = repo.fetch_unpublished()
        confirmed_events = [e for e in unpub if e["event_type"] == "OrderConfirmed"]
        assert len(confirmed_events) == 1
        assert confirmed_events[0]["payload"]["orderId"] == order.id
        assert confirmed_events[0]["payload"]["amountPaise"] == order.amount_paise

    def test_amount_paise_includes_tax_and_delivery_fee(self, service, repo, pricing_client):
        # net_payable=55.0 (base 50 + tax 3 + delivery 2), quantity=2 —
        # regression test: amountPaise must reflect the *quote's*
        # net_payable, not a naive catalogPrice * quantity recomputation.
        pricing_client.net_payable = 55.0
        _materialize(service, quantity=2)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.amount_paise == 5500
        catalog_price_paise = 50.0 * 100  # what a naive base-price-only calc would give
        assert order.amount_paise > catalog_price_paise * 2 / 2  # tax+fee inclusive, not base-only
        assert order.amount_paise == round(55.0 * 100)

    def test_insufficient_balance_marks_payment_failed(self, service, repo, wallet_client):
        wallet_client.result_status = "INSUFFICIENT_BALANCE"
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert order.failure_reason == "INSUFFICIENT_BALANCE"
        failed_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"
        ]
        assert len(failed_events) == 1
        assert failed_events[0]["payload"]["reason"] == "INSUFFICIENT_BALANCE"

    def test_wallet_not_active_marks_payment_failed(self, service, repo, wallet_client):
        wallet_client.result_status = "WALLET_NOT_ACTIVE"
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert order.failure_reason == "WALLET_NOT_ACTIVE"

    def test_wallet_transport_failure_leaves_order_created_and_raises(
        self, service, repo, wallet_client
    ):
        wallet_client.raise_unavailable = True
        with pytest.raises(WalletUnavailableError):
            _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order is not None
        assert order.status == OrderStatus.CREATED
        assert repo.fetch_unpublished() == []

    def test_pricing_unavailable_creates_no_order_and_raises(self, service, repo, pricing_client):
        pricing_client.raise_unavailable = True
        with pytest.raises(PricingUnavailableError):
            _materialize(service)
        assert repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE) is None

    def test_product_pricing_unknown_marks_payment_failed_product_unavailable(
        self, service, repo, pricing_client, wallet_client
    ):
        pricing_client.raise_product_unknown = True
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert order.failure_reason == "PRODUCT_UNAVAILABLE"
        assert order.amount_paise == 0  # never priced
        assert wallet_client.calls == []  # never reached the debit step
        failed_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"
        ]
        assert failed_events[0]["payload"]["reason"] == "PRODUCT_UNAVAILABLE"

    def test_user_unavailable_creates_no_order_and_raises(self, service, repo, user_client):
        user_client.raise_unavailable = True
        with pytest.raises(AddressLookupUnavailableError):
            _materialize(service)
        assert repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE) is None

    def test_no_address_on_file_marks_payment_failed_delivery_address_unknown(
        self, service, repo, user_client, pricing_client, wallet_client
    ):
        user_client.address_states["user-1"] = None
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert order.failure_reason == "DELIVERY_ADDRESS_UNKNOWN"
        assert order.amount_paise == 0
        assert wallet_client.calls == []
        assert repo.fetch_unpublished()[0]["payload"]["reason"] == "DELIVERY_ADDRESS_UNKNOWN"


class TestRedelivery:
    def test_redelivered_message_for_confirmed_order_is_noop(
        self, service, repo, pricing_client, wallet_client
    ):
        _materialize(service)
        first_order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert first_order.status == OrderStatus.CONFIRMED

        _materialize(service)  # redelivered

        assert wallet_client.calls == [
            ("user-1", first_order.id, first_order.amount_paise)
        ]  # not called twice
        confirmed_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"
        ]
        assert len(confirmed_events) == 1  # not duplicated

    def test_redelivered_message_for_payment_failed_order_is_noop(
        self, service, repo, wallet_client
    ):
        wallet_client.result_status = "INSUFFICIENT_BALANCE"
        _materialize(service)
        _materialize(service)  # redelivered
        assert len(wallet_client.calls) == 1

    def test_redelivered_message_for_pre_pricing_failure_is_noop(
        self, service, repo, user_client, pricing_client, wallet_client
    ):
        # Regression: DELIVERY_ADDRESS_UNKNOWN/PRODUCT_UNAVAILABLE are now
        # inserted directly as a terminal PAYMENT_FAILED row (one atomic
        # write, no CREATED intermediate state) — a redelivery must see
        # the order already resolved and no-op, never re-attempt the
        # user-address lookup or a debit.
        user_client.address_states["user-1"] = None
        _materialize(service)
        _materialize(service)  # redelivered
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.status == OrderStatus.PAYMENT_FAILED
        assert wallet_client.calls == []
        failed_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "OrderPaymentFailed"
        ]
        assert len(failed_events) == 1  # not duplicated

    def test_crash_between_insert_and_debit_resumes_at_debit_not_reinsert(
        self, service, repo, pricing_client, wallet_client
    ):
        # Simulate the crash: an Order row exists (CREATED) but the debit
        # never happened, e.g. a prior process died mid-materialize.
        from domain.models import Order

        repo.insert_created(
            Order(
                id="ord_precreated",
                user_id="user-1",
                subscription_id="sub-1",
                product_id="prod-1",
                quantity=2,
                amount_paise=5500,
                delivery_date=DELIVERY_DATE,
                status=OrderStatus.CREATED,
            )
        )
        _materialize(service)  # the redelivered SubscriptionOrderDue
        # Resumed at the debit step for the *existing* order — never
        # re-quoted or re-inserted a second row.
        assert wallet_client.calls == [("user-1", "ord_precreated", 5500)]
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        assert order.id == "ord_precreated"
        assert order.status == OrderStatus.CONFIRMED


class TestSqsResumeLease:
    """MA-143 FR-5 — the SQS crash-resume path honours the sweep's lease."""

    def _precreate(self, repo):
        from domain.models import Order

        repo.insert_created(
            Order(
                id="ord_precreated",
                user_id="user-1",
                subscription_id="sub-1",
                product_id="prod-1",
                quantity=2,
                amount_paise=5500,
                delivery_date=DELIVERY_DATE,
                status=OrderStatus.CREATED,
            )
        )

    def test_leased_by_sweep_raises_busy_without_debiting(self, service, repo, wallet_client):
        from domain.exceptions import OrderBusyError

        self._precreate(repo)
        assert repo.claim_order("ord_precreated", "sweep:x", 120)
        with pytest.raises(OrderBusyError):
            _materialize(service, claim_owner="sqs:m1")
        assert wallet_client.calls == []

    def test_unleased_resumes_and_releases(self, service, repo, wallet_client):
        self._precreate(repo)
        _materialize(service, claim_owner="sqs:m1")
        order = repo.get("ord_precreated")
        assert order.status == OrderStatus.CONFIRMED
        assert order.claim_owner is None
        assert len(wallet_client.calls) == 1

    def test_wallet_down_releases_the_lease_and_reraises(self, service, repo, wallet_client):
        wallet_client.raise_unavailable = True
        self._precreate(repo)
        with pytest.raises(WalletUnavailableError):
            _materialize(service, claim_owner="sqs:m1")
        order = repo.get("ord_precreated")
        assert order.status == OrderStatus.CREATED
        assert order.claim_owner is None
        assert order.sweep_attempts == 0  # SQS retries never spend the sweep budget

    def test_redelivery_for_escalated_order_is_a_no_op(self, service, repo, wallet_client):
        self._precreate(repo)
        repo.claim_order("ord_precreated", "sweep:x", 120)
        repo.close_order("ord_precreated", "sweep:x", "CUTOFF_PASSED")
        _materialize(service, claim_owner="sqs:m1")
        assert wallet_client.calls == []
        assert repo.get("ord_precreated").status == OrderStatus.NEEDS_ATTENTION

    def test_debit_refused_voided_releases_and_returns(self, service, repo, wallet_client, caplog):
        # FR-5: the sweep voided this order past its deadline; ack, never charge.
        from datetime import UTC, datetime

        self._precreate(repo)
        wallet_client.voided["ord_precreated"] = datetime.now(UTC)
        with caplog.at_level("WARNING"):
            _materialize(service, claim_owner="sqs:m1")
        order = repo.get("ord_precreated")
        assert order.status == OrderStatus.CREATED  # the sweep finishes the close
        assert order.claim_owner is None
        assert "order.debit_refused_voided" in caplog.text
        assert repo.fetch_unpublished() == []


# --- MA-154: customer cancellation ---------------------------------------

# Cancel deadline for DELIVERY_DATE: 20:00 IST the day before = 14:30 UTC.
_CUTOFF = datetime(2026, 1, 31, 14, 30, tzinfo=UTC)
_BEFORE = _CUTOFF - timedelta(seconds=1)


def _confirmed(service, repo, **overrides):
    """A CONFIRMED (debited) subscription order for DELIVERY_DATE."""
    _materialize(service, **overrides)
    order = repo.get_by_subscription_and_date(
        overrides.get("subscription_id", "sub-1"), DELIVERY_DATE
    )
    assert order.status == OrderStatus.CONFIRMED
    return order


def _cancel(service, order_id, *, user_id="user-1", reason="NOT_HOME", now=_BEFORE):
    return service.cancel(order_id, user_id, reason, now, "corr-cancel")


def _cancelled_events(repo):
    return [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderCancelled"]


def _set(engine, order_id, **values):
    with engine.begin() as conn:
        conn.execute(orders_table.update().where(orders_table.c.id == order_id).values(**values))


class TestCancel:
    def test_cancels_refunds_and_publishes_one_event(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        body = _cancel(service, order.id)

        stored = repo.get(order.id)
        assert stored.status == OrderStatus.CANCELLED
        assert stored.failure_reason == "CUSTOMER_CANCELLED"
        assert stored.cancel_reason == CancelReason.NOT_HOME
        assert stored.refund_state == RefundState.REFUNDED
        assert stored.refunded_at is not None
        assert wallet_client.refund_calls == [
            ("user-1", order.id, "cancel", order.amount_paise)
        ]
        assert body["status"] == "CANCELLED"
        assert body["refundState"] == "REFUNDED"
        assert body["cancelReason"] == "NOT_HOME"
        assert body["cancellableUntil"] is None
        assert body["cancelledAt"]

        [event] = _cancelled_events(repo)
        payload = event["payload"]
        jsonschema.validate(payload, load_schema("OrderCancelled"))
        assert payload["refundState"] == "PENDING"  # the value at cancel time
        assert payload["cancelReason"] == "NOT_HOME"
        assert payload["items"] == [{"productId": "prod-1", "quantity": 2}]
        assert payload["correlationId"] == "corr-cancel"

    def test_no_reason_is_stored_as_null(self, service, repo):
        order = _confirmed(service, repo)
        _cancel(service, order.id, reason=None)
        assert repo.get(order.id).cancel_reason is None
        jsonschema.validate(_cancelled_events(repo)[0]["payload"], load_schema("OrderCancelled"))

    @pytest.mark.parametrize("reason", ["LOL", "not_home", 5, ""])
    def test_unknown_reason_is_a_validation_error(self, service, repo, reason):
        order = _confirmed(service, repo)
        with pytest.raises(ValidationError):
            _cancel(service, order.id, reason=reason)
        assert repo.get(order.id).status == OrderStatus.CONFIRMED

    def test_at_the_cutoff_is_refused(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        with pytest.raises(CutoffPassedError) as exc:
            _cancel(service, order.id, now=_CUTOFF)
        assert exc.value.details == {"cancellableUntil": "2026-01-31T20:00:00+05:30"}
        assert repo.get(order.id).status == OrderStatus.CONFIRMED
        assert wallet_client.refund_calls == []

    def test_one_second_before_the_cutoff_succeeds(self, service, repo):
        order = _confirmed(service, repo)
        assert _cancel(service, order.id, now=_BEFORE)["status"] == "CANCELLED"

    def test_another_users_order_is_not_found(self, service, repo):
        order = _confirmed(service, repo)
        with pytest.raises(OrderNotFoundError):
            _cancel(service, order.id, user_id="user-2")
        with pytest.raises(OrderNotFoundError):
            _cancel(service, "ord_unknown")

    def test_payment_failed_is_not_cancellable(self, service, repo, wallet_client):
        wallet_client.result_status = "INSUFFICIENT_BALANCE"
        _materialize(service)
        order = repo.get_by_subscription_and_date("sub-1", DELIVERY_DATE)
        with pytest.raises(OrderNotCancellableError) as exc:
            _cancel(service, order.id)
        assert exc.value.details == {"status": "PAYMENT_FAILED"}

    @pytest.mark.parametrize(
        ("status", "reason"), [("CREATED", None), ("CANCELLED", "CUTOFF_PASSED")]
    )
    def test_other_statuses_are_not_cancellable(self, service, repo, engine, status, reason):
        order = _confirmed(service, repo)
        _set(engine, order.id, status=status, failure_reason=reason)
        with pytest.raises(OrderNotCancellableError):
            _cancel(service, order.id)

    def test_replay_after_refund_calls_wallet_once(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        first = _cancel(service, order.id)
        # A replay is answered even after the cut-off: it changes nothing.
        second = _cancel(service, order.id, now=_CUTOFF + timedelta(hours=1))
        assert second["status"] == first["status"] == "CANCELLED"
        assert second["refundState"] == "REFUNDED"
        assert len(wallet_client.refund_calls) == 1
        assert len(_cancelled_events(repo)) == 1

    def test_replay_while_pending_finishes_the_refund(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        wallet_client.refund_exception = WalletUnavailableError("down")
        assert _cancel(service, order.id)["refundState"] == "PENDING"
        wallet_client.refund_exception = None
        assert _cancel(service, order.id)["refundState"] == "REFUNDED"
        assert len(wallet_client.refund_calls) == 2

    def test_wallet_unavailable_still_cancels_with_refund_pending(
        self, service, repo, wallet_client
    ):
        order = _confirmed(service, repo)
        wallet_client.refund_exception = WalletUnavailableError("down")
        body = _cancel(service, order.id)
        assert body["status"] == "CANCELLED"
        assert body["refundState"] == "PENDING"

    def test_no_debit_found_means_no_refund_required(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        wallet_client.refund_exception = DebitNotFoundError("none")
        assert _cancel(service, order.id)["refundState"] == "NOT_REQUIRED"

    def test_refund_data_error_stays_pending(self, service, repo, wallet_client):
        order = _confirmed(service, repo)
        wallet_client.refund_exception = RefundExceedsDebitError("bug")
        assert _cancel(service, order.id)["refundState"] == "PENDING"

    def test_zero_amount_order_needs_no_refund(self, service, repo, engine, wallet_client):
        order = _confirmed(service, repo)
        _set(engine, order.id, amount_paise=0)
        body = _cancel(service, order.id)
        assert body["refundState"] == "NOT_REQUIRED"
        assert wallet_client.refund_calls == []
        payload = _cancelled_events(repo)[0]["payload"]
        assert payload["refundState"] == "NOT_REQUIRED"
        jsonschema.validate(payload, load_schema("OrderCancelled"))

    def test_lost_race_replays_the_winner(self, service, repo, monkeypatch):
        order = _confirmed(service, repo)
        _cancel(service, order.id)
        # As if this request read the order just before the winner committed.
        stale = repo.get(order.id)
        stale.status, stale.failure_reason = OrderStatus.CONFIRMED, None
        reads = iter([stale])
        real_get = repo.get
        monkeypatch.setattr(repo, "get", lambda oid: next(reads, None) or real_get(oid))
        body = _cancel(service, order.id)
        assert body["status"] == "CANCELLED"
        assert len(_cancelled_events(repo)) == 1


class TestCancellableUntil:
    def test_set_for_confirmed_before_the_cutoff(self, service, repo):
        order = _confirmed(service, repo)
        body = service.get(order.id, "user-1", now=_BEFORE)
        assert body["cancellableUntil"] == "2026-01-31T20:00:00+05:30"
        assert body["refundState"] is None
        assert body["cancelReason"] is None
        assert body["cancelledAt"] is None

    def test_null_at_and_after_the_cutoff(self, service, repo):
        order = _confirmed(service, repo)
        assert service.get(order.id, "user-1", now=_CUTOFF)["cancellableUntil"] is None

    def test_null_for_other_statuses(self, service, wallet_client):
        wallet_client.result_status = "INSUFFICIENT_BALANCE"
        _materialize(service)
        listed = service.list_for_user("user-1", None, None, None, now=_BEFORE)
        assert [o["cancellableUntil"] for o in listed["items"]] == [None]
