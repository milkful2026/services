import pytest
import responses

from adapters.catalog_client import HttpCatalogClient
from domain.exceptions import (
    CatalogIntegrationError,
    ProductPricingUnknownError,
    ServiceUnavailableError,
)


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(
        base_url="http://catalog.test",
        timeout_seconds=1.0,
        max_retries=1,
        backoff_base_seconds=0.0,
    )


@responses.activate
def test_get_price_returns_the_products_price():
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"requestId": "r1", "status": "success", "data": {"id": "cow-milk", "price": 68}},
        status=200,
    )

    price = _client().get_price("cow-milk")

    assert price == 68.0


@responses.activate
def test_get_price_maps_404_to_product_pricing_unknown():
    responses.add(
        responses.GET,
        "http://catalog.test/products/ghost",
        json={
            "requestId": "r1",
            "status": "error",
            "data": {"errorCode": "PRODUCT_NOT_FOUND", "message": "no such product"},
        },
        status=404,
    )

    with pytest.raises(ProductPricingUnknownError) as exc_info:
        _client().get_price("ghost")
    assert exc_info.value.details["productId"] == "ghost"


@responses.activate
def test_get_price_retries_then_fails_closed_on_repeated_5xx():
    responses.add(responses.GET, "http://catalog.test/products/cow-milk", status=500)
    responses.add(responses.GET, "http://catalog.test/products/cow-milk", status=500)

    with pytest.raises(ServiceUnavailableError):
        _client().get_price("cow-milk")

    # max_retries=1 -> exactly 2 attempts total.
    assert len(responses.calls) == 2


@responses.activate
def test_get_price_succeeds_on_retry_after_one_transient_failure():
    responses.add(responses.GET, "http://catalog.test/products/cow-milk", status=500)
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"requestId": "r1", "status": "success", "data": {"id": "cow-milk", "price": 68}},
        status=200,
    )

    price = _client().get_price("cow-milk")

    assert price == 68.0
    assert len(responses.calls) == 2


@responses.activate
def test_get_price_does_not_retry_a_4xx_other_than_404():
    responses.add(
        responses.GET,
        "http://catalog.test/products/",
        json={"requestId": "r1", "status": "error", "data": {}},
        status=422,
    )

    with pytest.raises(CatalogIntegrationError):
        _client().get_price("")

    # Not retried: a 422 means Catalog rejected this exact request, so
    # retrying it unchanged can't produce a different response.
    assert len(responses.calls) == 1


@responses.activate
def test_get_price_maps_malformed_200_body_to_catalog_integration_error():
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"requestId": "r1", "status": "success", "data": {}},
        status=200,
    )

    with pytest.raises(CatalogIntegrationError):
        _client().get_price("cow-milk")


@responses.activate
def test_get_price_sends_the_given_correlation_id():
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"requestId": "r1", "status": "success", "data": {"id": "cow-milk", "price": 68}},
        status=200,
    )

    _client().get_price("cow-milk", correlation_id="corr-123")

    assert responses.calls[0].request.headers["x-correlation-id"] == "corr-123"
