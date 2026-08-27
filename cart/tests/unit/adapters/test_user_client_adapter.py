import pytest
import responses
from botocore.credentials import Credentials

from adapters.user_client_adapter import HttpUserClient
from domain.exceptions import AddressLookupUnavailableError


def _client(max_retries: int = 1) -> HttpUserClient:
    return HttpUserClient(
        base_url="http://user.test",
        region_name="ap-south-1",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch):
    # SigV4Auth needs *some* credentials to produce a signature — a fixed,
    # fake pair is enough to exercise real signing logic deterministically
    # without touching any real AWS account, same spirit as bootstrap.py's
    # own dummy "local"/"local" pair for moto.
    fake_credentials = Credentials("AKIAFAKE", "fakesecret")
    monkeypatch.setattr(
        "adapters.user_client_adapter.boto3.Session",
        lambda: type(
            "FakeSession", (), {"get_credentials": staticmethod(lambda: fake_credentials)}
        )(),
    )


@responses.activate
def test_signs_the_request_with_sigv4():
    responses.add(
        responses.GET,
        "http://user.test/v1/internal/users/address-state",
        json={"data": {"defaultAddressState": "Karnataka"}},
        status=200,
    )

    _client().get_delivery_address_state("some-cognito-sub")

    sent_headers = responses.calls[0].request.headers
    assert sent_headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert "execute-api" in sent_headers["Authorization"]
    assert "x-amz-date" in {k.lower() for k in sent_headers}


@responses.activate
def test_returns_default_address_state_from_200_body():
    responses.add(
        responses.GET,
        "http://user.test/v1/internal/users/address-state",
        json={"data": {"defaultAddressState": "Karnataka"}},
        status=200,
    )

    assert _client().get_delivery_address_state("sub-1") == "Karnataka"


@responses.activate
def test_returns_none_when_no_default_address_state():
    responses.add(
        responses.GET,
        "http://user.test/v1/internal/users/address-state",
        json={"data": {"defaultAddressState": None}},
        status=200,
    )

    assert _client().get_delivery_address_state("sub-1") is None


@responses.activate
def test_maps_404_to_none():
    # No profile found for this cognito_sub — treated the same as "no
    # default address set" from Cart's own perspective (cart_service.py's
    # DeliveryAddressRequiredError covers both identically).
    responses.add(responses.GET, "http://user.test/v1/internal/users/address-state", status=404)

    assert _client().get_delivery_address_state("ghost-sub") is None


@responses.activate
def test_retries_then_raises_address_lookup_unavailable_on_repeated_5xx():
    responses.add(responses.GET, "http://user.test/v1/internal/users/address-state", status=503)
    responses.add(responses.GET, "http://user.test/v1/internal/users/address-state", status=503)

    with pytest.raises(AddressLookupUnavailableError):
        _client().get_delivery_address_state("sub-1")

    assert len(responses.calls) == 2  # max_retries=1 -> exactly 2 attempts


_MALFORMED_200_BODIES = [
    pytest.param({"body": "<html>502</html>", "content_type": "text/html"}, id="not-json"),
    pytest.param({"json": {"error": "nope"}}, id="no-data-key"),
    pytest.param({"json": {"data": "not-an-object"}}, id="data-not-a-dict"),
]


@pytest.mark.parametrize("body_kwargs", _MALFORMED_200_BODIES)
@responses.activate
def test_malformed_200_body_becomes_address_lookup_unavailable_not_raw(body_kwargs):
    for _ in range(2):
        responses.add(
            responses.GET,
            "http://user.test/v1/internal/users/address-state",
            status=200,
            **body_kwargs,
        )

    with pytest.raises(AddressLookupUnavailableError):
        _client().get_delivery_address_state("sub-1")

    assert len(responses.calls) == 2  # retried like any other transport failure


def test_no_credentials_available_raises_address_lookup_unavailable(monkeypatch):
    monkeypatch.setattr(
        "adapters.user_client_adapter.boto3.Session",
        lambda: type("FakeSession", (), {"get_credentials": staticmethod(lambda: None)})(),
    )

    with pytest.raises(AddressLookupUnavailableError):
        _client(max_retries=0).get_delivery_address_state("sub-1")
