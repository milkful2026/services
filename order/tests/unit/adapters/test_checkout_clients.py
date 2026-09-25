"""MA-136 — HTTP adapters checkout depends on: Cart's internal routes
(SigV4), Subscription's internal create, Wallet's balance read."""

import json
from datetime import date

import pytest
import responses

from adapters.cart_client_adapter import HttpCartClient
from adapters.subscription_client_adapter import HttpSubscriptionClient
from adapters.wallet_client_adapter import HttpWalletClient
from domain.exceptions import (
    CartUnavailableError,
    CartVersionConflictError,
    SubscriptionRejectedError,
    SubscriptionUnavailableError,
    WalletBalanceUnavailableError,
)

_CART = "http://cart.test/cart/internal/users/user-1"


def _cart(max_retries=1):
    return HttpCartClient("http://cart.test", "ap-south-1", 1.0, max_retries, 0.0)


@responses.activate
def test_cart_get_is_sigv4_signed_and_parsed():
    responses.add(
        responses.GET, _CART,
        json={"data": {"items": [{"id": "li-1"}], "cartVersion": 4}}, status=200,
    )

    snapshot = _cart().get_cart("user-1")

    assert snapshot.cart_version == 4
    assert snapshot.items == [{"id": "li-1"}]
    assert responses.calls[0].request.headers["Authorization"].startswith("AWS4-HMAC-SHA256")


@responses.activate
def test_cart_remove_sends_the_exact_signed_body():
    responses.add(
        responses.POST, f"{_CART}/remove-items",
        json={"data": {"items": [], "cartVersion": 5}}, status=200,
    )

    _cart().remove_items("user-1", ["li-1"], 4, "chk_1")

    body = json.loads(responses.calls[0].request.body)
    assert body == {"itemIds": ["li-1"], "ifVersion": 4, "reason": "CHECKOUT",
                    "checkoutId": "chk_1"}
    assert "AWS4-HMAC-SHA256" in responses.calls[0].request.headers["Authorization"]


@responses.activate
def test_cart_409_is_a_conflict_not_retried():
    responses.add(responses.POST, f"{_CART}/remove-items", status=409, json={})

    with pytest.raises(CartVersionConflictError):
        _cart(max_retries=2).remove_items("user-1", ["li-1"], 4, "chk_1")

    assert len(responses.calls) == 1


@responses.activate
def test_cart_5xx_is_retried_then_unavailable():
    responses.add(responses.GET, _CART, status=502)

    with pytest.raises(CartUnavailableError):
        _cart(max_retries=1).get_cart("user-1")

    assert len(responses.calls) == 2


_SUBS = "http://subscription.test/internal/subscriptions"


def _create(client):
    return client.create(
        user_id="user-1", product_id="cow-milk", quantity=2, schedule_type="DAILY",
        start_date=date(2026, 9, 27), slot_id="slot-am",
        idempotency_key="checkout:chk_1:li-2", correlation_id="corr-1",
    )


def _subs(max_retries=1):
    return HttpSubscriptionClient("http://subscription.test", 1.0, max_retries, 0.0)


@responses.activate
def test_subscription_create_sends_the_line_and_parses_the_result():
    responses.add(
        responses.POST, _SUBS, status=200,
        json={"data": {"subscriptionId": "sub_1", "status": "ACTIVE",
                       "nextDeliveryDate": "2026-09-27"}},
    )

    result = _create(_subs())

    assert result == {"subscriptionId": "sub_1", "nextDeliveryDate": "2026-09-27"}
    body = json.loads(responses.calls[0].request.body)
    assert body["userId"] == "user-1"
    assert body["schedule"] == {"type": "DAILY"}
    assert body["startDate"] == "2026-09-27"
    assert body["idempotencyKey"] == "checkout:chk_1:li-2"


@pytest.mark.parametrize("status", [400, 422])
@responses.activate
def test_subscription_4xx_is_a_line_rejection_with_its_code(status):
    responses.add(
        responses.POST, _SUBS, status=status,
        json={"data": {"errorCode": "PRODUCT_NOT_ELIGIBLE", "message": "no"}},
    )

    with pytest.raises(SubscriptionRejectedError) as exc:
        _create(_subs(max_retries=2))

    assert exc.value.reason == "PRODUCT_NOT_ELIGIBLE"
    assert len(responses.calls) == 1


@responses.activate
def test_subscription_5xx_is_retried_then_unavailable():
    responses.add(responses.POST, _SUBS, status=503)

    with pytest.raises(SubscriptionUnavailableError):
        _create(_subs(max_retries=1))

    assert len(responses.calls) == 2


@responses.activate
def test_wallet_balance_read_returns_paise():
    responses.add(
        responses.GET, "http://wallet.test/wallet/internal/balance",
        json={"data": {"balancePaise": 12_345, "status": "ACTIVE"}}, status=200,
    )

    client = HttpWalletClient("http://wallet.test", 1.0, 0, 0.0)

    assert client.get_balance("user-1") == 12_345
    assert responses.calls[0].request.params == {"userId": "user-1"}


@responses.activate
def test_wallet_balance_read_unavailable_after_retries():
    responses.add(responses.GET, "http://wallet.test/wallet/internal/balance", status=500)

    with pytest.raises(WalletBalanceUnavailableError):
        HttpWalletClient("http://wallet.test", 1.0, 1, 0.0).get_balance("user-1")
