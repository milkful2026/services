import pytest
import responses

from adapters.wallet_client_adapter import HttpWalletClient
from domain.exceptions import WalletCheckUnavailableError

_URL = "http://wallet.test/wallet/internal/balance"


def _client(max_retries: int = 1) -> HttpWalletClient:
    return HttpWalletClient(
        base_url="http://wallet.test",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@responses.activate
def test_get_balance_returns_paise_and_sends_user_id():
    responses.add(
        responses.GET,
        _URL,
        json={"data": {"balancePaise": 45_000, "status": "ACTIVE"}},
        status=200,
    )

    assert _client().get_balance("sub-123") == 45_000
    assert responses.calls[0].request.params == {"userId": "sub-123"}


@responses.activate
def test_get_balance_retries_a_5xx_then_succeeds():
    responses.add(responses.GET, _URL, status=503)
    responses.add(responses.GET, _URL, json={"data": {"balancePaise": 100}}, status=200)

    assert _client(max_retries=1).get_balance("sub-123") == 100
    assert len(responses.calls) == 2


@responses.activate
def test_get_balance_raises_unavailable_after_retries():
    responses.add(responses.GET, _URL, status=500)

    with pytest.raises(WalletCheckUnavailableError):
        _client(max_retries=1).get_balance("sub-123")

    assert len(responses.calls) == 2


@responses.activate
def test_get_balance_malformed_body_is_unavailable():
    responses.add(responses.GET, _URL, json={"data": {}}, status=200)

    with pytest.raises(WalletCheckUnavailableError):
        _client(max_retries=0).get_balance("sub-123")


def test_get_balance_without_base_url_fails_closed():
    with pytest.raises(WalletCheckUnavailableError):
        HttpWalletClient().get_balance("sub-123")


def test_set_correlation_id_does_not_raise():
    client = HttpWalletClient()
    client.set_correlation_id("corr-1")
