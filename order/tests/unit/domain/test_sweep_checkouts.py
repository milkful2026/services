"""MA-144 — the sweep's abandoned-checkout pass and the lease/PD-1 rules on
the customer checkout path, against the SQLite repository and the
conftest fakes."""

from datetime import UTC, datetime, timedelta

import pytest

from adapters.order_repository import checkouts_table
from domain.checkout_models import CheckoutStatus, CheckoutStep
from domain.cutoff import IST
from domain.exceptions import (
    CheckoutCancelledError,
    CheckoutIncompleteError,
    CheckoutInProgressError,
    CheckoutNeedsAttentionError,
    StoredCheckoutFailureError,
)
from domain.models import ChargeState, OrderStatus
from domain.sweep_service import SweepService

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=IST)  # checkout placed; delivery 2026-09-26
BEFORE_CUTOFF = datetime(2026, 9, 25, 19, 59, 59, tzinfo=IST)
AT_CUTOFF = datetime(2026, 9, 25, 20, 0, 0, tzinfo=IST)
MAX_ATTEMPTS = 3


class FakeMetrics:
    def __init__(self):
        self.emitted = []

    def emit(self, name, **dimensions):
        self.emitted.append((name, dimensions))


@pytest.fixture
def metrics():
    return FakeMetrics()


@pytest.fixture
def sweep(repo, service, checkout_service, metrics, wallet_client):
    return SweepService(
        repo,
        service,
        metrics,
        owner="sweep:test",
        wallet_client=wallet_client,
        charge_deadline_hour_ist=23,
        subscription_order_stale_seconds=900,
        max_attempts=MAX_ATTEMPTS,
        lease_seconds=120,
        batch_size=50,
        checkout_service=checkout_service,
        checkout_stale_seconds=600,
    )


def _one_time(line_id="li-1"):
    return {"id": line_id, "productId": "buffalo-milk", "quantity": 1,
            "frequency": "ONE_TIME", "startDate": None, "slotId": None}


def _daily(line_id="li-2", product_id="cow-milk"):
    return {"id": line_id, "productId": product_id, "quantity": 2,
            "frequency": "DAILY", "startDate": "2026-09-27", "slotId": "slot-am"}


def _checkout(svc, key="key-00000001", version=3, now=NOW):
    return svc.checkout(
        user_id="user-1",
        idempotency_key=key,
        cart_version=version,
        expected_pay_now_paise=None,
        correlation_id="corr-1",
        now=now,
    )


def _age(engine, minutes=20, **columns):
    """Make every checkout look untouched for `minutes`."""
    with engine.begin() as conn:
        conn.execute(
            checkouts_table.update().values(
                updated_at=datetime.now(UTC) - timedelta(minutes=minutes), **columns
            )
        )


def _abandon(checkout_service, engine, **columns):
    """Run a checkout that fails partway (the caller set a fake failing),
    then make it look abandoned. Returns the checkout id."""
    with pytest.raises(CheckoutIncompleteError) as info:
        _checkout(checkout_service)
    _age(engine, **columns)
    return info.value.details["checkoutId"]


def _only_checkout(repo, checkout_id):
    return repo.get_checkout_by_id(checkout_id)


# --- resume ------------------------------------------------------------------


def test_abandoned_after_payment_is_completed(
    sweep, checkout_service, repo, engine, cart_client, wallet_client, subscription_client
):
    # Ticket AC 1.
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine)
    assert _only_checkout(repo, checkout_id).step == CheckoutStep.PAID

    subscription_client.unavailable_for = set()
    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.COMPLETED
    assert checkout.claim_owner is None
    assert [r.status for r in checkout.subscription_results] == ["CREATED"]
    assert cart_client.items == []
    assert len(wallet_client.calls) == 1
    assert counts["completed"] == 1


def test_abandoned_before_charge_is_charged_exactly_once_when_wallet_returns(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    # Ticket AC 2.
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False

    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.COMPLETED
    assert len(wallet_client.debited) == 1
    assert repo.get(checkout.order_id).status == OrderStatus.CONFIRMED


def test_declined_charge_ends_payment_failed(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False
    wallet_client.result_status = "INSUFFICIENT_BALANCE"

    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.PAYMENT_FAILED
    assert counts["payment_failed"] == 1


# --- PD-1 ----------------------------------------------------------------------


def _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False
    wallet_client.calls.clear()
    return checkout_id


def test_uncharged_past_cutoff_is_cancelled_without_charging(
    sweep, checkout_service, repo, engine, cart_client, wallet_client, metrics
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)

    counts = sweep.sweep_checkouts("corr", AT_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.CANCELLED
    assert checkout.result["error"]["errorCode"] == "CHECKOUT_CANCELLED"
    order = repo.get(checkout.order_id)
    assert order.status == OrderStatus.CANCELLED
    assert order.failure_reason == "CUTOFF_PASSED"
    assert order.charge_state == ChargeState.NOT_CHARGED
    assert wallet_client.void_calls == [order.id]  # proven uncharged by the void
    assert wallet_client.calls == []  # never charged
    assert cart_client.remove_calls == []  # cart untouched
    assert repo.fetch_unpublished() == []  # no event for a never-confirmed order
    assert counts["cancelled"] == 1
    assert ("sweep.checkout.cancelled", {}) in metrics.emitted


def test_one_second_before_cutoff_is_charged_not_cancelled(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)
    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.COMPLETED
    assert wallet_client.void_calls == []  # not eligible, so Wallet isn't even asked
    assert len(wallet_client.calls) == 1


def test_lost_response_debit_found_is_completed_not_cancelled(
    sweep, checkout_service, repo, engine, cart_client, wallet_client, metrics
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    order_id = _only_checkout(repo, checkout_id).order_id
    # The debit landed in Wallet, but its response was lost.
    from domain.models import DebitLookup

    wallet_client.debited[order_id] = DebitLookup(5500, 94500, datetime.now(UTC))

    sweep.sweep_checkouts("corr", AT_CUTOFF)

    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.COMPLETED
    assert repo.get(order_id).status == OrderStatus.CONFIRMED
    assert ("sweep.checkout.charged_after_cutoff", {}) in metrics.emitted


def test_void_unavailable_never_cancels(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    wallet_client.raise_void_unavailable = True

    counts = sweep.sweep_checkouts("corr", AT_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.IN_PROGRESS
    assert checkout.sweep_attempts == 1
    assert checkout.claim_owner is None
    assert repo.get(checkout.order_id).status == OrderStatus.CREATED
    assert counts["failed_attempt"] == 1


# --- budget exhausted ----------------------------------------------------------


def test_exhausted_before_charge_escalates_checkout_and_order(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)

    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.NEEDS_ATTENTION
    assert checkout.last_sweep_error == "CHARGE_UNKNOWN"
    order = repo.get(checkout.order_id)
    assert order.status == OrderStatus.NEEDS_ATTENTION
    assert order.charge_state == ChargeState.UNKNOWN  # for the settle pass
    assert counts["escalated"] == 1


def test_exhausted_after_payment_completes_with_lines_left_in_cart(
    sweep, checkout_service, repo, engine, cart_client, subscription_client, metrics
):
    # PD-2.
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)

    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.COMPLETED
    [line] = checkout.subscription_results
    assert (line.status, line.reason) == ("FAILED", "SUBSCRIPTION_UNAVAILABLE")
    assert cart_client.remove_calls[-1][0] == ["li-1"]  # the paid line only
    assert [i["id"] for i in cart_client.items] == ["li-2"]
    assert counts["completed_partial"] == 1
    assert ("sweep.checkout.escalated", {"reason": "SUBSCRIPTIONS_ABANDONED"}) in metrics.emitted
    assert repo.get_carried_keys("user-1") == {
        "li-2": (f"checkout:{checkout_id}:li-2", checkout_id)
    }
    assert "carried" not in str(checkout.result)  # internal only


def test_exhausted_at_cart_clear_escalates_and_keeps_the_paid_order(
    sweep, checkout_service, repo, engine, cart_client
):
    cart_client.items = [_one_time()]
    cart_client.remove_unavailable = True
    checkout_id = _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)
    assert _only_checkout(repo, checkout_id).step == CheckoutStep.SUBSCRIPTIONS_DONE

    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.NEEDS_ATTENTION
    assert checkout.last_sweep_error == "CART_CLEAR_FAILED"
    assert repo.get(checkout.order_id).status == OrderStatus.CONFIRMED


def test_customer_can_order_again_after_escalation(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    # Ticket AC 3: leaving IN_PROGRESS frees the one-live-checkout lock.
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    wallet_client.raise_unavailable = False
    result = _checkout(checkout_service, key="key-00000002", version=cart_client.cart_version)
    assert result["status"] == "COMPLETED"


# --- lease ---------------------------------------------------------------------


def test_lost_claim_makes_no_calls(
    sweep, checkout_service, repo, engine, cart_client, wallet_client, monkeypatch
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    monkeypatch.setattr(repo, "claim_checkout", lambda *a, **k: False)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)
    assert wallet_client.calls == []
    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.IN_PROGRESS


def test_lease_lost_mid_run_stops_without_writing(
    sweep, checkout_service, repo, engine, cart_client, subscription_client, monkeypatch
):
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine)
    subscription_client.unavailable_for = set()
    subscription_client.calls.clear()
    monkeypatch.setattr(repo, "renew_checkout", lambda *a, **k: False)

    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    assert counts["lease_lost"] == 1
    assert subscription_client.calls == []
    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.IN_PROGRESS
    assert checkout.sweep_attempts == 0


# --- customer path (FR-5, FR-6) ------------------------------------------------


def _claimed_by_sweep(repo, checkout_id):
    assert repo.claim_checkout(checkout_id, "sweep:x", 120)


def test_same_key_retry_while_sweep_holds_lease_is_409_with_retry_after(
    checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False
    _claimed_by_sweep(repo, checkout_id)

    with pytest.raises(CheckoutInProgressError) as info:
        _checkout(checkout_service)
    retry_after = info.value.details["retryAfterSeconds"]
    assert isinstance(retry_after, int) and retry_after >= 1
    assert len(wallet_client.debited) == 0


def test_takeover_while_sweep_holds_lease_is_409(
    checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    checkout_id = _abandon(checkout_service, engine)
    wallet_client.raise_unavailable = False
    _claimed_by_sweep(repo, checkout_id)

    with pytest.raises(CheckoutInProgressError) as info:
        _checkout(checkout_service, key="key-00000002")
    assert info.value.details["retryAfterSeconds"] >= 1


def test_same_key_retry_after_cutoff_is_cancelled_not_charged(
    checkout_service, repo, engine, cart_client, wallet_client
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)

    with pytest.raises(StoredCheckoutFailureError) as info:
        _checkout(checkout_service, now=AT_CUTOFF)
    assert info.value.error_code == "CHECKOUT_CANCELLED"
    assert info.value.http_status == 409
    assert wallet_client.calls == []
    assert cart_client.remove_calls == []
    # Replaying the same key again returns the same stored error.
    with pytest.raises(StoredCheckoutFailureError):
        _checkout(checkout_service, now=AT_CUTOFF)
    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.CANCELLED


def test_takeover_after_cutoff_cancels_old_and_starts_fresh(
    checkout_service, repo, engine, cart_client, wallet_client
):
    old_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)

    result = _checkout(checkout_service, key="key-00000002", now=AT_CUTOFF)

    assert _only_checkout(repo, old_id).status == CheckoutStatus.CANCELLED
    assert result["status"] == "COMPLETED"
    assert result["checkoutId"] != old_id
    assert len(wallet_client.calls) == 1  # only the new checkout was charged


def test_replay_of_escalated_checkout_is_needs_attention(
    sweep, checkout_service, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    with pytest.raises(CheckoutNeedsAttentionError):
        _checkout(checkout_service)


def test_incomplete_request_hands_its_lease_back(
    checkout_service, repo, engine, cart_client, wallet_client
):
    cart_client.items = [_one_time()]
    wallet_client.raise_unavailable = True
    with pytest.raises(CheckoutIncompleteError) as info:
        _checkout(checkout_service)
    checkout = _only_checkout(repo, info.value.details["checkoutId"])
    assert checkout.claim_owner is None and checkout.claimed_until is None


# --- PD-1 for a ₹0 order ---------------------------------------------------------


def _abandoned_free(checkout_service, engine, cart_client, pricing_client, monkeypatch):
    """A ₹0 one-time order left CREATED at STARTED (the request died
    before _charge confirmed it)."""
    cart_client.items = [_one_time()]
    pricing_client.net_payable = 0.0

    def crash(*args, **kwargs):
        raise CheckoutIncompleteError("crashed", {"checkoutId": "?"})

    with monkeypatch.context() as m:
        m.setattr(checkout_service, "_charge", crash)
        with pytest.raises(CheckoutIncompleteError):
            _checkout(checkout_service)
    _age(engine)
    [row] = engine.connect().execute(checkouts_table.select()).fetchall()
    return row.id


def test_free_order_before_cutoff_is_confirmed_without_wallet(
    sweep, checkout_service, repo, engine, cart_client, pricing_client, wallet_client,
    monkeypatch,
):
    checkout_id = _abandoned_free(checkout_service, engine, cart_client, pricing_client,
                                  monkeypatch)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)
    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.COMPLETED
    assert repo.get(checkout.order_id).status == OrderStatus.CONFIRMED
    assert wallet_client.calls == [] and wallet_client.void_calls == []


def test_free_order_after_cutoff_is_cancelled_without_wallet_or_event(
    sweep, checkout_service, repo, engine, cart_client, pricing_client, wallet_client,
    monkeypatch,
):
    # Regression for the PR #25 finding: never confirmed for a missed date.
    checkout_id = _abandoned_free(checkout_service, engine, cart_client, pricing_client,
                                  monkeypatch)
    counts = sweep.sweep_checkouts("corr", AT_CUTOFF)
    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.CANCELLED
    order = repo.get(checkout.order_id)
    assert (order.status, order.charge_state) == (OrderStatus.CANCELLED, ChargeState.NOT_CHARGED)
    assert wallet_client.calls == [] and wallet_client.void_calls == []
    assert [e for e in repo.fetch_unpublished() if e["event_type"] == "OrderConfirmed"] == []
    assert counts["cancelled"] == 1


# --- DEBIT_VOIDED from the charge (FR-3) ---------------------------------------


def test_debit_refused_voided_finishes_the_cancel(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    # Another worker voided the order, then crashed before cancelling it.
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    order_id = _only_checkout(repo, checkout_id).order_id
    wallet_client.voided[order_id] = datetime.now(UTC)

    counts = sweep.sweep_checkouts("corr", BEFORE_CUTOFF)

    checkout = _only_checkout(repo, checkout_id)
    assert checkout.status == CheckoutStatus.CANCELLED
    assert checkout.result["error"]["errorCode"] == "CHECKOUT_CANCELLED"
    order = repo.get(order_id)
    assert (order.status, order.charge_state) == (OrderStatus.CANCELLED, ChargeState.NOT_CHARGED)
    assert order_id not in wallet_client.debited
    assert repo.fetch_unpublished() == []
    assert counts["cancelled"] == 1


def test_customer_debit_refused_voided_is_409_cancelled(
    checkout_service, repo, engine, cart_client, wallet_client
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    order_id = _only_checkout(repo, checkout_id).order_id
    wallet_client.voided[order_id] = datetime.now(UTC)

    with pytest.raises(CheckoutCancelledError) as info:
        _checkout(checkout_service, now=BEFORE_CUTOFF)
    assert info.value.http_status == 409
    assert _only_checkout(repo, checkout_id).status == CheckoutStatus.CANCELLED
    with pytest.raises(StoredCheckoutFailureError) as replay:
        _checkout(checkout_service, now=BEFORE_CUTOFF)
    assert replay.value.error_code == "CHECKOUT_CANCELLED"


def test_same_key_retry_after_cutoff_sees_a_landed_debit_and_completes(
    checkout_service, repo, engine, cart_client, wallet_client
):
    # The first request timed out but its debit committed inside Wallet.
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    order_id = _only_checkout(repo, checkout_id).order_id
    from domain.models import DebitLookup

    wallet_client.debited[order_id] = DebitLookup(5500, 94500, datetime.now(UTC))
    result = _checkout(checkout_service, now=AT_CUTOFF)
    assert result["status"] == "COMPLETED"
    assert repo.get(order_id).status == OrderStatus.CONFIRMED
    assert order_id not in wallet_client.voided


# --- carried subscription keys (FR-4a) -----------------------------------------


def _pd2(sweep, checkout_service, engine, cart_client, subscription_client):
    """Checkout A pays, its subscription line fails for the whole budget."""
    cart_client.items = [_one_time(), _daily()]
    subscription_client.unavailable_for = {"cow-milk"}
    checkout_id = _abandon(checkout_service, engine, sweep_attempts=MAX_ATTEMPTS - 1)
    sweep.sweep_checkouts("corr", BEFORE_CUTOFF)
    subscription_client.unavailable_for = set()
    subscription_client.calls.clear()
    return checkout_id


def test_next_checkout_reuses_the_carried_key_and_replays(
    sweep, checkout_service, repo, engine, cart_client, subscription_client
):
    a = _pd2(sweep, checkout_service, engine, cart_client, subscription_client)
    # A's create had actually landed; only its response was lost.
    landed = subscription_client.create(
        user_id="user-1", product_id="cow-milk", quantity=2, schedule_type="DAILY",
        start_date=_only_checkout(repo, a).lines[1].start_date, slot_id="slot-am",
        idempotency_key=f"checkout:{a}:li-2", correlation_id="c",
    )
    subscription_client.calls.clear()

    result = _checkout(checkout_service, key="key-00000002", version=cart_client.cart_version)

    [call] = subscription_client.calls
    assert call["idempotency_key"] == f"checkout:{a}:li-2"  # not checkout:{B}:li-2
    [line] = result["subscriptions"]
    assert (line["status"], line["subscriptionId"]) == ("CREATED", landed["subscriptionId"])
    assert cart_client.items == []
    assert repo.get_carried_keys("user-1") == {}


def test_rejected_create_forgets_the_carried_key(
    sweep, checkout_service, repo, engine, cart_client, subscription_client
):
    _pd2(sweep, checkout_service, engine, cart_client, subscription_client)
    subscription_client.reject = {"cow-milk": "PRODUCT_NOT_ELIGIBLE"}
    _checkout(checkout_service, key="key-00000002", version=cart_client.cart_version)
    assert repo.get_carried_keys("user-1") == {}


def test_transient_failure_keeps_the_original_key(
    sweep, checkout_service, repo, engine, cart_client, subscription_client
):
    a = _pd2(sweep, checkout_service, engine, cart_client, subscription_client)
    subscription_client.unavailable_for = {"cow-milk"}
    with pytest.raises(CheckoutIncompleteError):
        _checkout(checkout_service, key="key-00000002", version=cart_client.cart_version)
    assert repo.get_carried_keys("user-1") == {"li-2": (f"checkout:{a}:li-2", a)}


def test_edited_line_keeps_the_replayed_subscription_and_logs(
    sweep, checkout_service, repo, engine, cart_client, subscription_client, caplog
):
    _pd2(sweep, checkout_service, engine, cart_client, subscription_client)
    cart_client.items[0]["quantity"] = 5  # edited since checkout A
    with caplog.at_level("INFO"):
        result = _checkout(checkout_service, key="key-00000002",
                           version=cart_client.cart_version)
    assert result["subscriptions"][0]["status"] == "CREATED"
    assert "checkout.subscription_replayed_with_changes" in caplog.text
