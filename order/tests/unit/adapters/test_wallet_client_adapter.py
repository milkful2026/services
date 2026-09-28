import pytest
import responses

from adapters.wallet_client_adapter import HttpWalletClient
from domain.exceptions import WalletUnavailableError


def _client(max_retries: int = 1) -> HttpWalletClient:
    return HttpWalletClient(
        base_url="http://wallet.test",
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,
    )


@responses.activate
def test_debited_reads_balance_after_paise():
    responses.add(
        responses.POST,
        "http://wallet.test/wallet/internal/debit",
        json={"data": {"status": "DEBITED", "balanceAfterPaise": 5000}},
        status=200,
    )

    result = _client().debit("user-1", "ord-1", 1000, "corr-1")
    assert result.status == "DEBITED"
    assert result.balance_after_paise == 5000


@responses.activate
def test_insufficient_balance_reads_balance_paise_not_balance_after_paise():
    # Regression: Wallet's serialize_debit_outcome uses `balancePaise`
    # for INSUFFICIENT_BALANCE (not `balanceAfterPaise`, which is only
    # the DEBITED key) — this adapter used to only ever read
    # balanceAfterPaise, silently getting None for this status.
    responses.add(
        responses.POST,
        "http://wallet.test/wallet/internal/debit",
        json={
            "data": {
                "status": "INSUFFICIENT_BALANCE",
                "balancePaise": 500,
                "requiredPaise": 1000,
            }
        },
        status=200,
    )

    result = _client().debit("user-1", "ord-1", 1000, "corr-1")
    assert result.status == "INSUFFICIENT_BALANCE"
    assert result.balance_after_paise == 500


@responses.activate
def test_wallet_not_active_has_no_balance():
    responses.add(
        responses.POST,
        "http://wallet.test/wallet/internal/debit",
        json={"data": {"status": "WALLET_NOT_ACTIVE"}},
        status=200,
    )

    result = _client().debit("user-1", "ord-1", 1000, "corr-1")
    assert result.status == "WALLET_NOT_ACTIVE"
    assert result.balance_after_paise is None


# --- MA-142: get_debit ---

_LOOKUP_URL = "http://wallet.test/wallet/internal/debits/ord-1"
_FOUND = {
    "data": {
        "orderId": "ord-1",
        "status": "DEBITED",
        "amountPaise": 10820,
        "balanceAfterPaise": 34180,
        "debitedAt": "2026-09-28T14:03:11.412000+00:00",
        "walletId": "wal_1",
    }
}


@responses.activate
def test_get_debit_found_returns_lookup():
    responses.add(responses.GET, _LOOKUP_URL, json=_FOUND, status=200)
    lookup = _client().get_debit("ord-1")
    assert lookup.amount_paise == 10820
    assert lookup.balance_after_paise == 34180
    assert lookup.debited_at.year == 2026


@responses.activate
def test_get_debit_not_found_returns_none():
    responses.add(
        responses.GET,
        _LOOKUP_URL,
        json={"data": {"errorCode": "DEBIT_NOT_FOUND", "message": "x"}},
        status=404,
    )
    assert _client().get_debit("ord-1") is None


@responses.activate
def test_get_debit_retries_5xx_then_succeeds():
    responses.add(responses.GET, _LOOKUP_URL, status=503)
    responses.add(responses.GET, _LOOKUP_URL, json=_FOUND, status=200)
    assert _client(max_retries=2).get_debit("ord-1").amount_paise == 10820


@responses.activate
def test_get_debit_5xx_every_attempt_is_unavailable_not_none():
    responses.add(responses.GET, _LOOKUP_URL, status=503)
    with pytest.raises(WalletUnavailableError):
        _client(max_retries=1).get_debit("ord-1")


@responses.activate
def test_get_debit_unexpected_4xx_is_unavailable_and_not_retried():
    responses.add(
        responses.GET,
        _LOOKUP_URL,
        json={"data": {"errorCode": "VALIDATION_ERROR", "message": "x"}},
        status=400,
    )
    with pytest.raises(WalletUnavailableError):
        _client(max_retries=2).get_debit("ord-1")
    assert len(responses.calls) == 1


@responses.activate
def test_get_debit_404_without_debit_not_found_code_is_unavailable():
    # e.g. an old Wallet without the route: FastAPI's own 404 must never
    # be read as "not debited".
    responses.add(responses.GET, _LOOKUP_URL, json={"detail": "Not Found"}, status=404)
    with pytest.raises(WalletUnavailableError):
        _client().get_debit("ord-1")
