import pytest
import responses

from adapters.pricing_client_adapter import HttpPricingClient
from domain.exceptions import PricingUnavailableError, ProductPricingUnknownError


def _client(max_retries: int = 1) -> HttpPricingClient:
    return HttpPricingClient(
        base_url="http://pricing.test",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@responses.activate
def test_product_pricing_unknown_recognized():
    responses.add(
        responses.POST,
        "http://pricing.test/pricing/quote",
        json={"data": {"errorCode": "PRODUCT_PRICING_UNKNOWN"}},
        status=404,
    )

    with pytest.raises(ProductPricingUnknownError):
        _client().quote("prod-1", 2, "MH")


@pytest.mark.parametrize(
    "body_kwargs",
    [
        pytest.param({"body": "<html>404</html>", "content_type": "text/html"}, id="not-json"),
        pytest.param({"json": ["not", "a", "dict"]}, id="json-not-a-dict"),
        pytest.param({"json": {"data": "not-a-dict-either"}}, id="data-not-a-dict"),
        pytest.param({"json": None}, id="json-null"),
    ],
)
@responses.activate
def test_malformed_404_body_raises_pricing_unavailable_not_raw(body_kwargs):
    # Regression: a 404 whose body is valid JSON but not the expected
    # {"data": {"errorCode": ...}} shape (e.g. a bare list/string/null
    # from a misbehaving proxy or an API Gateway default error page)
    # must fall through to the generic retryable-then-PricingUnavailableError
    # path, not raise an uncaught AttributeError from chained .get() calls.
    for _ in range(2):
        responses.add(
            responses.POST, "http://pricing.test/pricing/quote", status=404, **body_kwargs
        )

    with pytest.raises(PricingUnavailableError):
        _client().quote("prod-1", 2, "MH")


@responses.activate
def test_unrecognized_404_error_code_raises_pricing_unavailable():
    for _ in range(2):
        responses.add(
            responses.POST,
            "http://pricing.test/pricing/quote",
            json={"data": {"errorCode": "SOMETHING_ELSE"}},
            status=404,
        )

    with pytest.raises(PricingUnavailableError):
        _client().quote("prod-1", 2, "MH")
