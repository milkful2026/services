import pytest
import responses

from adapters.wallet_client_adapter import HttpWalletClient
from domain.exceptions import DebitVoidedError, WalletUnavailableError
from domain.models import DebitLookup, Voided


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


@responses.activate
def test_get_debit_voided_returns_voided():
    responses.add(
        responses.GET,
        _LOOKUP_URL,
        json={"data": {"orderId": "ord-1", "status": "VOIDED",
                       "voidedAt": "2026-09-28T14:03:11.412000+00:00"}},
        status=200,
    )
    assert isinstance(_client().get_debit("ord-1"), Voided)


# --- MA-142: void_debit (bodies exactly as Wallet's envelope sends them) ---

_VOID_URL = "http://wallet.test/wallet/internal/debits/ord-1/void"
_VOIDED = {
    "requestId": "r",
    "status": "success",
    "data": {"orderId": "ord-1", "status": "VOIDED",
             "voidedAt": "2026-09-28T14:03:11.412000+00:00"},
}
# Wallet's error envelope flattens `details` into `data`.
_ALREADY_DEBITED = {
    "requestId": "r",
    "status": "error",
    "data": {
        "errorCode": "ALREADY_DEBITED",
        "message": "order 'ord-1' was already debited",
        "orderId": "ord-1",
        "status": "DEBITED",
        "amountPaise": 10820,
        "balanceAfterPaise": 34180,
        "debitedAt": "2026-09-28T14:03:11.412000+00:00",
        "walletId": "wal_1",
    },
}


@responses.activate
def test_void_voided_returns_voided_and_sends_the_user():
    responses.add(responses.POST, _VOID_URL, json=_VOIDED, status=200)
    outcome = _client().void_debit("user-1", "ord-1")
    assert isinstance(outcome, Voided)
    assert outcome.voided_at.year == 2026
    assert responses.calls[0].request.body == b'{"userId": "user-1"}'


@responses.activate
def test_void_already_debited_returns_the_debit():
    responses.add(responses.POST, _VOID_URL, json=_ALREADY_DEBITED, status=409)
    outcome = _client().void_debit("user-1", "ord-1")
    assert outcome == DebitLookup(
        amount_paise=10820,
        balance_after_paise=34180,
        debited_at=outcome.debited_at,
    )
    assert outcome.amount_paise == 10820


@responses.activate
def test_void_retries_5xx_then_succeeds():
    responses.add(responses.POST, _VOID_URL, status=503)
    responses.add(responses.POST, _VOID_URL, status=503)
    responses.add(responses.POST, _VOID_URL, json=_VOIDED, status=200)
    assert isinstance(_client(max_retries=2).void_debit("user-1", "ord-1"), Voided)


@responses.activate
def test_void_5xx_every_attempt_is_unavailable_never_voided():
    responses.add(responses.POST, _VOID_URL, status=503)
    with pytest.raises(WalletUnavailableError):
        _client(max_retries=2).void_debit("user-1", "ord-1")


@responses.activate
def test_void_unexpected_4xx_is_unavailable_and_not_retried():
    responses.add(
        responses.POST,
        _VOID_URL,
        json={"data": {"errorCode": "VALIDATION_ERROR", "message": "x"}},
        status=400,
    )
    with pytest.raises(WalletUnavailableError):
        _client(max_retries=2).void_debit("user-1", "ord-1")
    assert len(responses.calls) == 1


@responses.activate
def test_void_route_missing_is_unavailable_not_voided():
    responses.add(responses.POST, _VOID_URL, json={"detail": "Not Found"}, status=404)
    with pytest.raises(WalletUnavailableError):
        _client().void_debit("user-1", "ord-1")


# --- MA-142 FR-3: debit refused ---


@responses.activate
def test_debit_voided_raises_and_is_not_retried():
    responses.add(
        responses.POST,
        "http://wallet.test/wallet/internal/debit",
        json={"data": {"errorCode": "DEBIT_VOIDED", "message": "x", "orderId": "ord-1",
                       "voidedAt": "2026-09-28T14:03:11.412000+00:00"}},
        status=409,
    )
    with pytest.raises(DebitVoidedError):
        _client(max_retries=2).debit("user-1", "ord-1", 1000, "corr-1")
    assert len(responses.calls) == 1
