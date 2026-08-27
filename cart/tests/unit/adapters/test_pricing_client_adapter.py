import pytest
import responses

from adapters.pricing_client_adapter import HttpPricingClient
from domain.exceptions import PricingUnavailableError


def _client(max_retries: int = 1) -> HttpPricingClient:
    return HttpPricingClient(
        base_url="http://pricing.test",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


_ITEMS = [{"product_id": "cow-milk", "quantity": 2, "frequency": "ONE_TIME"}]


@responses.activate
def test_quote_parses_full_response_body():
    responses.add(
        responses.POST,
        "http://pricing.test/pricing/quote",
        json={
            "data": {
                "basePrice": 136.0,
                "taxAmount": 6.8,
                "taxRate": 5.0,
                "deliveryFee": 20.0,
                "netPayable": 162.8,
                "monthlyEstimate": None,
                "discountAmount": None,
                "appliedOfferId": None,
            }
        },
        status=200,
    )

    quote = _client().quote(_ITEMS, delivery_state="Karnataka")

    assert quote.base_price == 136.0
    assert quote.tax_amount == 6.8
    assert quote.tax_rate == 5.0
    assert quote.delivery_fee == 20.0
    assert quote.net_payable == 162.8
    assert quote.monthly_estimate is None
    assert quote.discount_amount is None
    assert quote.applied_offer_id is None


@responses.activate
def test_quote_request_body_matches_pricing_contract():
    responses.add(
        responses.POST,
        "http://pricing.test/pricing/quote",
        json={
            "data": {
                "basePrice": 68.0,
                "taxAmount": 3.4,
                "taxRate": 5.0,
                "deliveryFee": 20.0,
                "netPayable": 91.4,
            }
        },
        status=200,
    )

    _client().quote(_ITEMS, delivery_state="Karnataka", offer_code="WELCOME10")

    sent = responses.calls[0].request
    import json

    body = json.loads(sent.body)
    assert body == {
        "items": [{"productId": "cow-milk", "quantity": 2, "frequency": "ONE_TIME"}],
        "deliveryState": "Karnataka",
        "offerCode": "WELCOME10",
    }


@responses.activate
def test_400_from_pricing_raises_pricing_unavailable_without_retry():
    responses.add(
        responses.POST,
        "http://pricing.test/pricing/quote",
        json={"data": {"errorCode": "VALIDATION_ERROR", "message": "deliveryState is required"}},
        status=400,
    )

    with pytest.raises(PricingUnavailableError):
        _client().quote(_ITEMS, delivery_state="")

    assert len(responses.calls) == 1  # not retried — this is a request-shape bug, not transient


@responses.activate
def test_non_json_400_body_still_raises_pricing_unavailable_not_raw():
    # A 400 from API Gateway request validation / a proxy / a WAF has a
    # non-JSON body — response.json() would raise, and that must not
    # escape unmapped.
    responses.add(
        responses.POST,
        "http://pricing.test/pricing/quote",
        body="<html>400 Bad Request</html>",
        content_type="text/html",
        status=400,
    )

    with pytest.raises(PricingUnavailableError):
        _client().quote(_ITEMS, delivery_state="Karnataka")

    assert len(responses.calls) == 1  # still not retried


@responses.activate
def test_retries_then_raises_pricing_unavailable_on_repeated_5xx():
    responses.add(responses.POST, "http://pricing.test/pricing/quote", status=503)
    responses.add(responses.POST, "http://pricing.test/pricing/quote", status=503)

    with pytest.raises(PricingUnavailableError):
        _client().quote(_ITEMS, delivery_state="Karnataka")

    assert len(responses.calls) == 2  # max_retries=1 -> exactly 2 attempts


_MALFORMED_200_BODIES = [
    pytest.param({"body": "<html>502</html>", "content_type": "text/html"}, id="not-json"),
    pytest.param({"json": {"data": {"basePrice": 1.0}}}, id="missing-required-fields"),
    pytest.param({"json": {"error": "nope"}}, id="no-data-key"),
]


@pytest.mark.parametrize("body_kwargs", _MALFORMED_200_BODIES)
@responses.activate
def test_malformed_200_body_becomes_pricing_unavailable_not_raw(body_kwargs):
    for _ in range(2):
        responses.add(
            responses.POST, "http://pricing.test/pricing/quote", status=200, **body_kwargs
        )

    with pytest.raises(PricingUnavailableError):
        _client().quote(_ITEMS, delivery_state="Karnataka")

    assert len(responses.calls) == 2
