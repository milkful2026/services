import pytest
import responses

from adapters.catalog_client_adapter import HttpCatalogClient
from domain.exceptions import StockCheckUnavailableError


def _client(max_retries: int = 1) -> HttpCatalogClient:
    return HttpCatalogClient(
        base_url="http://catalog.test",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@responses.activate
def test_returns_available_quantity_from_200_body():
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"data": {"id": "cow-milk", "availableQuantity": 42}},
        status=200,
    )

    assert _client().get_available_quantity("cow-milk") == 42


@responses.activate
def test_returns_none_when_available_quantity_field_absent():
    # Valid and expected today: Catalog's own available_quantity addition
    # (MA-120 §7) isn't implemented yet, so the field is simply missing.
    responses.add(
        responses.GET,
        "http://catalog.test/products/cow-milk",
        json={"data": {"id": "cow-milk"}},
        status=200,
    )

    assert _client().get_available_quantity("cow-milk") is None


@responses.activate
def test_maps_404_to_none():
    responses.add(responses.GET, "http://catalog.test/products/ghost", status=404)

    assert _client().get_available_quantity("ghost") is None


@responses.activate
def test_retries_then_raises_stock_check_unavailable_on_repeated_5xx():
    responses.add(responses.GET, "http://catalog.test/products/cow-milk", status=500)
    responses.add(responses.GET, "http://catalog.test/products/cow-milk", status=500)

    with pytest.raises(StockCheckUnavailableError):
        _client().get_available_quantity("cow-milk")

    assert len(responses.calls) == 2  # max_retries=1 -> exactly 2 attempts


_MALFORMED_200_BODIES = [
    pytest.param({"body": "<html>502</html>", "content_type": "text/html"}, id="not-json"),
    pytest.param({"json": ["not", "an", "object"]}, id="top-level-list"),
    pytest.param({"json": {"error": "nope"}}, id="no-data-key"),
    pytest.param({"json": {"data": ["not", "an", "object"]}}, id="data-is-list"),
    pytest.param({"json": {"data": "oops"}}, id="data-is-string"),
]


@pytest.mark.parametrize("body_kwargs", _MALFORMED_200_BODIES)
@responses.activate
def test_malformed_200_body_becomes_stock_check_unavailable_not_raw(body_kwargs):
    # A 200 whose body isn't the {"data": {...}} envelope must surface as
    # StockCheckUnavailableError (the adapter's only documented failure
    # mode), never as a raw JSONDecodeError / KeyError / AttributeError.
    for _ in range(2):  # both attempts, since max_retries=1
        responses.add(
            responses.GET, "http://catalog.test/products/cow-milk", status=200, **body_kwargs
        )

    with pytest.raises(StockCheckUnavailableError):
        _client().get_available_quantity("cow-milk")

    assert len(responses.calls) == 2  # retried like a 5xx, then failed closed
