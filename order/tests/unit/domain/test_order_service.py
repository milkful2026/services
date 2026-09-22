from datetime import date

import pytest

from domain.exceptions import (
    AddressLookupUnavailableError,
    PricingUnavailableError,
    WalletUnavailableError,
)
from domain.models import OrderStatus

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
