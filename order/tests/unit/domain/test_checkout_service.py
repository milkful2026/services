"""MA-136 — CheckoutService against fakes for every port and a real
SQLite repository. Covers each FR-3 rejection (nothing persisted), every
charge outcome, resume from each step, same-key replay, and the
double-charge guards."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from adapters.order_repository import checkouts_table
from domain.checkout_models import CheckoutStatus, CheckoutStep
from domain.exceptions import (
    CartChangedError,
    CartEmptyError,
    CheckoutIncompleteError,
    CheckoutInProgressError,
    DeliveryAddressUnknownError,
    DependencyUnavailableError,
    InsufficientBalanceError,
    LineInvalidError,
    PriceChangedError,
    StoredCheckoutFailureError,
    WalletNotActiveError,
)
from domain.models import OrderSource, OrderStatus

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 25, 14, 0, tzinfo=IST)  # before the 20:00 cut-off
TOMORROW = date(2026, 9, 26)


def _one_time(line_id="li-1", product_id="buffalo-milk", quantity=1):
    return {"id": line_id, "productId": product_id, "quantity": quantity,
            "frequency": "ONE_TIME", "startDate": None, "slotId": None}


def _daily(line_id="li-2", product_id="cow-milk", quantity=2,
           start="2026-09-27T00:00:00.000", slot="slot-am", frequency="DAILY"):
    return {"id": line_id, "productId": product_id, "quantity": quantity,
            "frequency": frequency, "startDate": start, "slotId": slot}


def _checkout(svc, key="key-00000001", version=3, expected=None, now=NOW):
    return svc.checkout(
        user_id="user-1",
        idempotency_key=key,
        cart_version=version,
        expected_pay_now_paise=expected,
        correlation_id="corr-1",
        now=now,
    )


# --- happy paths --------------------------------------------------------------


def test_mixed_cart_charges_once_starts_subscription_and_clears_cart(
    checkout_service, repo, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_one_time(), _daily()]

    result = _checkout(checkout_service, expected=5500)

    order = result["order"]
    assert order["status"] == "CONFIRMED"
    assert order["amountPaise"] == 5500
    assert order["deliveryDate"] == TOMORROW.isoformat()
    assert order["items"] == [{"productId": "buffalo-milk", "quantity": 1}]
    assert wallet_client.calls == [("user-1", order["orderId"], 5500)]

    [sub] = result["subscriptions"]
    assert sub["status"] == "CREATED" and sub["lineId"] == "li-2"
    [call] = subscription_client.calls
    assert call["schedule_type"] == "DAILY"
    assert call["start_date"] == date(2026, 9, 27)  # ISO timestamp -> date
    assert call["slot_id"] == "slot-am"
    assert call["idempotency_key"] == f"checkout:{result['checkoutId']}:li-2"

    assert cart_client.remove_calls == [(["li-1", "li-2"], 3, result["checkoutId"])]
    assert cart_client.items == []
    assert result["walletBalanceAfterPaise"] == 100_000 - 5500

    stored = repo.get_checkout("user-1", "key-00000001")
    assert stored.status == CheckoutStatus.COMPLETED
    assert stored.result == result

    persisted = repo.get(order["orderId"])
    assert persisted.source == OrderSource.CHECKOUT
    assert persisted.checkout_id == result["checkoutId"]
    assert persisted.subscription_id is None


def test_confirmed_outbox_payload_describes_a_checkout_order(checkout_service, repo, cart_client):
    cart_client.items = [_one_time(quantity=3)]

    result = _checkout(checkout_service)

    [event] = [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"]
    assert event["payload"]["source"] == "CHECKOUT"
    assert event["payload"]["checkoutId"] == result["checkoutId"]
    assert event["payload"]["subscriptionId"] is None
    assert event["payload"]["items"] == [{"productId": "buffalo-milk", "quantity": 3}]


def test_one_time_only_cart_creates_no_subscriptions(
    checkout_service, cart_client, subscription_client
):
    cart_client.items = [_one_time()]

    result = _checkout(checkout_service)

    assert result["subscriptions"] == []
    assert subscription_client.calls == []
    assert cart_client.remove_calls[0][0] == ["li-1"]


def test_subscription_only_cart_charges_nothing_now(
    checkout_service, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_daily()]

    result = _checkout(checkout_service, expected=0)

    assert result["order"] is None
    assert wallet_client.calls == []
    assert [s["status"] for s in result["subscriptions"]] == ["CREATED"]
    assert result["walletBalanceAfterPaise"] == 100_000


def test_free_one_time_lines_still_get_a_confirmed_order(
    checkout_service, repo, cart_client, pricing_client, wallet_client
):
    # A 100% offer prices the one-time lines to ₹0 — the lines still leave
    # the cart, so there must still be an order recording the delivery.
    cart_client.items = [_one_time()]
    pricing_client.net_payable = 0.0

    result = _checkout(checkout_service, expected=0)

    order = result["order"]
    assert order["status"] == "CONFIRMED"
    assert order["amountPaise"] == 0
    assert order["items"] == [{"productId": "buffalo-milk", "quantity": 1}]
    assert wallet_client.calls == []  # no zero-amount debit
    assert cart_client.items == []
    assert [e["event_type"] for e in repo.fetch_unpublished()] == ["OrderConfirmed"]


def test_alternate_days_maps_to_its_schedule(checkout_service, cart_client, subscription_client):
    cart_client.items = [_daily(frequency="ALTERNATE_DAYS")]

    _checkout(checkout_service)

    assert subscription_client.calls[0]["schedule_type"] == "ALTERNATE_DAYS"


def test_delivery_date_after_cutoff_is_the_day_after_tomorrow(checkout_service, cart_client):
    cart_client.items = [_one_time()]

    result = _checkout(checkout_service, now=datetime(2026, 9, 25, 20, 0, tzinfo=IST))

    assert result["order"]["deliveryDate"] == "2026-09-27"


def test_delivery_date_uses_ist_not_the_callers_timezone(checkout_service, cart_client):
    cart_client.items = [_one_time()]
    # 15:00 UTC is 20:30 IST — past the cut-off.
    result = _checkout(checkout_service, now=datetime(2026, 9, 25, 15, 0, tzinfo=UTC))

    assert result["order"]["deliveryDate"] == "2026-09-27"


# --- FR-3 rejections: nothing persisted -----------------------------------------


def _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client):
    assert repo.get_checkout("user-1", "key-00000001") is None
    assert repo.get_live_checkout("user-1") is None
    assert wallet_client.calls == []
    assert subscription_client.calls == []
    assert cart_client.remove_calls == []


def test_empty_cart(checkout_service, repo, cart_client, wallet_client, subscription_client):
    with pytest.raises(CartEmptyError):
        _checkout(checkout_service)
    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


def test_cart_version_moved(checkout_service, repo, cart_client, wallet_client,
                            subscription_client):
    cart_client.items = [_one_time()]

    with pytest.raises(CartChangedError) as exc:
        _checkout(checkout_service, version=2)

    assert exc.value.details == {"cartVersion": 3}
    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (_daily(slot=None), "SLOT_MISSING"),
        (_daily(start="2026-09-24"), "START_DATE_PAST"),
        (_daily(start=None), "START_DATE_PAST"),
    ],
)
def test_invalid_subscription_lines(checkout_service, repo, cart_client, wallet_client,
                                    subscription_client, line, reason):
    cart_client.items = [_one_time(), line]

    with pytest.raises(LineInvalidError) as exc:
        _checkout(checkout_service)

    assert exc.value.details == {"lines": [{"lineId": "li-2", "reason": reason}]}
    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


def test_start_date_today_is_allowed(checkout_service, cart_client):
    cart_client.items = [_daily(start="2026-09-25")]

    _checkout(checkout_service)


def test_unknown_product_marks_only_that_line(checkout_service, cart_client, pricing_client):
    cart_client.items = [_one_time("li-1", "gone"), _one_time("li-3", "paneer")]
    pricing_client.raise_product_unknown = True
    pricing_client.unknown_product_id = "gone"

    with pytest.raises(LineInvalidError) as exc:
        _checkout(checkout_service)

    assert exc.value.details == {"lines": [{"lineId": "li-1", "reason": "PRODUCT_UNAVAILABLE"}]}


def test_no_delivery_address(checkout_service, cart_client, user_client):
    cart_client.items = [_one_time()]
    user_client.address_states = {}

    with pytest.raises(DeliveryAddressUnknownError):
        _checkout(checkout_service)


def test_price_changed_since_review(checkout_service, repo, cart_client, wallet_client,
                                    subscription_client):
    cart_client.items = [_one_time()]

    with pytest.raises(PriceChangedError) as exc:
        _checkout(checkout_service, expected=5000)

    assert exc.value.details == {"payNowPaise": 5500}
    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


def test_insufficient_balance_precheck_reports_shortfall(
    checkout_service, repo, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_one_time()]
    wallet_client.balance_paise = 2000

    with pytest.raises(InsufficientBalanceError) as exc:
        _checkout(checkout_service)

    assert exc.value.details == {
        "balancePaise": 2000, "requiredPaise": 5500, "shortfallPaise": 3500,
    }
    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


def test_subscription_lines_require_the_minimum_balance_on_top(checkout_service, cart_client,
                                                               wallet_client):
    cart_client.items = [_one_time(), _daily()]
    wallet_client.balance_paise = 50_000  # covers the one-time 5500, not +50000

    with pytest.raises(InsufficientBalanceError) as exc:
        _checkout(checkout_service)

    assert exc.value.details["requiredPaise"] == 55_500
    assert exc.value.details["shortfallPaise"] == 5500


@pytest.mark.parametrize("fail", ["cart", "user", "pricing", "wallet"])
def test_dependency_down_before_start_is_retryable_and_persists_nothing(
    checkout_service, repo, cart_client, user_client, pricing_client, wallet_client,
    subscription_client, fail,
):
    cart_client.items = [_one_time()]
    if fail == "cart":
        cart_client.raise_unavailable = True
    elif fail == "user":
        user_client.raise_unavailable = True
    elif fail == "pricing":
        pricing_client.raise_unavailable = True
    else:
        wallet_client.raise_balance_unavailable = True

    with pytest.raises(DependencyUnavailableError):
        _checkout(checkout_service)

    _assert_nothing_persisted(repo, wallet_client, subscription_client, cart_client)


def test_second_live_checkout_with_another_key_is_rejected(
    checkout_service, repo, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service, key="key-00000001")

    live = repo.get_live_checkout("user-1")
    with pytest.raises(CheckoutInProgressError) as exc_info:
        _checkout(checkout_service, key="key-00000002")
    # Names the live checkout, so the app can tell which one to resume.
    assert exc_info.value.details == {"checkoutId": live.id}


def _abandon(engine, checkout_id):
    with engine.begin() as conn:
        conn.execute(
            checkouts_table.update()
            .where(checkouts_table.c.id == checkout_id)
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=10))
        )


def test_abandoned_live_checkout_is_finished_by_a_new_key_instead_of_blocking_it(
    checkout_service, repo, engine, cart_client, wallet_client
):
    # The app got CHECKOUT_INCOMPLETE and then lost its Idempotency-Key.
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service, key="key-00000001")
    abandoned = repo.get_live_checkout("user-1")
    _abandon(engine, abandoned.id)
    wallet_client.raise_unavailable = False

    # The new key finishes the abandoned checkout (one charge, cart
    # cleared), then validates its own request against the cleared cart.
    with pytest.raises((CartEmptyError, CartChangedError)):
        _checkout(checkout_service, key="key-00000002")

    assert repo.get_live_checkout("user-1") is None
    assert repo.get_checkout("user-1", "key-00000001").status == CheckoutStatus.COMPLETED
    assert [c[1] for c in wallet_client.calls] == [abandoned.order_id] * 2  # 1 failed, 1 ok
    assert cart_client.items == []


def test_abandoned_live_checkout_that_is_declined_no_longer_blocks(
    checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service, key="key-00000001")
    _abandon(engine, repo.get_live_checkout("user-1").id)
    wallet_client.raise_unavailable = False
    wallet_client.result_status = "INSUFFICIENT_BALANCE"

    with pytest.raises(InsufficientBalanceError):
        _checkout(checkout_service, key="key-00000002")

    assert repo.get_checkout("user-1", "key-00000001").status == CheckoutStatus.PAYMENT_FAILED


# --- FR-5: charge outcomes --------------------------------------------------------


def test_declined_charge_stops_everything_and_replays_identically(
    checkout_service, repo, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_one_time(), _daily()]
    wallet_client.result_status = "INSUFFICIENT_BALANCE"  # race: pre-check passed

    with pytest.raises(InsufficientBalanceError) as first:
        _checkout(checkout_service)

    assert subscription_client.calls == []
    assert cart_client.remove_calls == []
    stored = repo.get_checkout("user-1", "key-00000001")
    assert stored.status == CheckoutStatus.PAYMENT_FAILED
    assert repo.get(stored.order_id).status == OrderStatus.PAYMENT_FAILED

    with pytest.raises(StoredCheckoutFailureError) as replay:
        _checkout(checkout_service)
    assert replay.value.error_code == "INSUFFICIENT_BALANCE"
    assert replay.value.http_status == 402
    assert replay.value.details == first.value.details
    assert len(wallet_client.calls) == 1  # replay never re-charges


def test_after_a_declined_charge_a_new_key_may_check_out(checkout_service, cart_client,
                                                         wallet_client):
    cart_client.items = [_one_time()]
    wallet_client.result_status = "INSUFFICIENT_BALANCE"
    with pytest.raises(InsufficientBalanceError):
        _checkout(checkout_service, key="key-00000001")

    wallet_client.result_status = "DEBITED"  # topped up
    result = _checkout(checkout_service, key="key-00000002")

    assert result["order"]["status"] == "CONFIRMED"


def test_wallet_not_active(checkout_service, cart_client, wallet_client):
    cart_client.items = [_one_time()]
    wallet_client.result_status = "WALLET_NOT_ACTIVE"

    with pytest.raises(WalletNotActiveError):
        _checkout(checkout_service)


def test_wallet_down_at_charge_then_resume_with_the_same_key(
    checkout_service, repo, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True

    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service)
    stored = repo.get_checkout("user-1", "key-00000001")
    assert (stored.status, stored.step) == (CheckoutStatus.IN_PROGRESS, CheckoutStep.STARTED)

    wallet_client.raise_unavailable = False
    result = _checkout(checkout_service)

    # Same order both times: Wallet's own order-id idempotency makes the
    # repeated debit call safe.
    assert {call[1] for call in wallet_client.calls} == {stored.order_id}
    assert result["order"]["orderId"] == stored.order_id
    assert result["order"]["status"] == "CONFIRMED"


def test_resume_after_confirm_never_debits_again(checkout_service, repo, cart_client,
                                                 wallet_client):
    cart_client.items = [_one_time()]
    cart_client.remove_unavailable = True
    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service)
    assert len(wallet_client.calls) == 1

    cart_client.remove_unavailable = False
    result = _checkout(checkout_service)

    assert len(wallet_client.calls) == 1
    assert result["order"]["status"] == "CONFIRMED"


# --- FR-6: subscriptions -----------------------------------------------------------


def test_rejected_subscription_line_does_not_block_the_rest(
    checkout_service, cart_client, subscription_client
):
    cart_client.items = [_one_time(), _daily("li-2", "paneer"), _daily("li-3", "cow-milk")]
    subscription_client.reject = {"paneer": "PRODUCT_NOT_ELIGIBLE"}

    result = _checkout(checkout_service)

    by_line = {s["lineId"]: s for s in result["subscriptions"]}
    assert by_line["li-2"] == {"lineId": "li-2", "productId": "paneer", "status": "FAILED",
                               "reason": "PRODUCT_NOT_ELIGIBLE"}
    assert by_line["li-3"]["status"] == "CREATED"
    assert result["order"]["status"] == "CONFIRMED"
    # The failed line stays in the cart.
    assert [i["id"] for i in cart_client.items] == ["li-2"]


def test_subscription_service_down_pauses_then_resumes_only_missing_lines(
    checkout_service, repo, cart_client, subscription_client, wallet_client
):
    cart_client.items = [_daily("li-2", "cow-milk"), _daily("li-3", "paneer")]
    subscription_client.unavailable_for = {"paneer"}

    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service)
    stored = repo.get_checkout("user-1", "key-00000001")
    assert stored.step == CheckoutStep.PAID
    assert [r.line_id for r in stored.subscription_results] == ["li-2"]

    subscription_client.unavailable_for = set()
    subscription_client.calls.clear()
    result = _checkout(checkout_service)

    assert [c["product_id"] for c in subscription_client.calls] == ["paneer"]
    assert [s["status"] for s in result["subscriptions"]] == ["CREATED", "CREATED"]


# --- FR-7: clearing the cart -----------------------------------------------------------


def test_cart_edited_elsewhere_mid_checkout_is_re_read_and_cleared(checkout_service,
                                                                   cart_client):
    cart_client.items = [_one_time()]
    cart_client.conflict_once = True

    _checkout(checkout_service)

    assert cart_client.items == []
    assert [call[1] for call in cart_client.remove_calls] == [3, 4]


def test_line_edited_elsewhere_mid_checkout_stays_in_the_cart(checkout_service, cart_client):
    cart_client.items = [_one_time(), _daily()]
    cart_client.conflict_once = True
    remove = cart_client.remove_items

    def edit_then_remove(*args, **kwargs):
        # Another device raises the one-time line's quantity 1 -> 5 while
        # this checkout (which charged for 1) is clearing the cart.
        cart_client.items[0]["quantity"] = 5
        return remove(*args, **kwargs)

    cart_client.remove_items = edit_then_remove

    result = _checkout(checkout_service)

    assert result["order"]["items"] == [{"productId": "buffalo-milk", "quantity": 1}]
    assert cart_client.remove_calls[-1][0] == ["li-2"]
    assert [(i["id"], i["quantity"]) for i in cart_client.items] == [("li-1", 5)]


def test_cart_down_at_clear_pauses_then_resume_finishes_without_side_effects(
    checkout_service, repo, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_one_time(), _daily()]
    cart_client.remove_unavailable = True

    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service)
    assert repo.get_checkout("user-1", "key-00000001").step == CheckoutStep.SUBSCRIPTIONS_DONE

    cart_client.remove_unavailable = False
    result = _checkout(checkout_service)

    assert len(wallet_client.calls) == 1
    assert len(subscription_client.calls) == 1
    assert result["status"] == "COMPLETED"
    assert cart_client.items == []


# --- FR-2: replay ---------------------------------------------------------------------


def test_completed_checkout_replays_without_any_calls(
    checkout_service, cart_client, wallet_client, subscription_client
):
    cart_client.items = [_one_time(), _daily()]
    first = _checkout(checkout_service)
    calls = (len(wallet_client.calls), len(subscription_client.calls),
             len(cart_client.remove_calls))

    again = _checkout(checkout_service)

    assert again == first
    assert (len(wallet_client.calls), len(subscription_client.calls),
            len(cart_client.remove_calls)) == calls


def test_new_key_after_success_sees_the_cleared_cart(checkout_service, cart_client):
    cart_client.items = [_one_time()]
    _checkout(checkout_service, key="key-00000001")

    with pytest.raises((CartEmptyError, CartChangedError)):
        _checkout(checkout_service, key="key-00000002")
