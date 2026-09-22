import responses

from adapters.wallet_client_adapter import HttpWalletClient


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
