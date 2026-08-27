import pytest

from adapters.wallet_client_adapter import HttpWalletClient
from domain.exceptions import WalletCheckUnavailableError


def test_get_balance_always_raises_wallet_check_unavailable():
    # No real contract exists to test a happy path against — MA-100
    # (Wallet Service) has no spec or implementation anywhere in this
    # repo. This is the entire test: the documented, only failure mode.
    with pytest.raises(WalletCheckUnavailableError):
        HttpWalletClient().get_balance("some-cognito-sub")


def test_set_correlation_id_does_not_raise():
    client = HttpWalletClient()
    client.set_correlation_id("corr-1")  # no-op today, but must not error
