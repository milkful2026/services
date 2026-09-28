"""MA-144 — the sweep's abandoned-checkout pass and the lease/PD-1 rules on
the customer checkout path, against the SQLite repository and the
conftest fakes."""

from datetime import UTC, datetime, timedelta

import pytest

from adapters.order_repository import checkouts_table
from domain.checkout_models import CheckoutStatus, CheckoutStep
from domain.cutoff import IST
from domain.exceptions import (
    CheckoutIncompleteError,
    CheckoutInProgressError,
    CheckoutNeedsAttentionError,
    StoredCheckoutFailureError,
)
from domain.models import OrderStatus
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
def sweep(repo, service, checkout_service, metrics):
    return SweepService(
        repo,
        service,
        metrics,
        owner="sweep:test",
        cutoff_hour_ist=20,
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
    assert wallet_client.lookup_calls == []  # not eligible, so Wallet isn't even asked
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


def test_lookup_unavailable_never_cancels(
    sweep, checkout_service, repo, engine, cart_client, wallet_client
):
    checkout_id = _abandoned_uncharged(checkout_service, engine, cart_client, wallet_client)
    wallet_client.raise_lookup_unavailable = True

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
    assert repo.get(checkout.order_id).status == OrderStatus.NEEDS_ATTENTION
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
