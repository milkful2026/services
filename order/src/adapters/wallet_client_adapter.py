"""HTTP client for Wallet Service's `POST /wallet/internal/debit`
(MA-130 FR-1) — the order-creation critical path's payment step.

No auth on this call (plain unauthenticated internal HTTP), matching
this service's general pattern (unlike User Service's SigV4-signed
address-state endpoint — see `user_client_adapter.py`).

`DEBITED` / `INSUFFICIENT_BALANCE` / `WALLET_NOT_ACTIVE` are all 200
responses per MA-130's own contract-shape fix — this adapter must never
treat any of the three as an HTTP error. A `503 WALLET_PROVISIONING_PENDING`
(MA-130's own review-added split for "no wallet row yet") is retried like
any other 5xx, and if still failing after retries surfaces as
`WalletUnavailableError` — the same transient/fail-closed posture as
`AddressLookupUnavailableError`/`PricingUnavailableError`, so a
subscription's first order right after registration retries via SQS
redelivery instead of permanently failing.

MA-153 adds `refund` (`POST /wallet/internal/refunds`), used by a
customer cancel (MA-154).

MA-142 adds the void (`POST /wallet/internal/debits/{orderId}/void`) and
the read-only lookup, and `debit` now raises `DebitVoidedError` on
`409 DEBIT_VOIDED` (never retried). Wallet's error envelope flattens an
error's `details` into `data`, next to `errorCode`."""

import logging
from datetime import datetime

import requests
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import (
    DebitNotFoundError,
    DebitVoidedError,
    OrderUserMismatchError,
    RefundExceedsDebitError,
    WalletBalanceUnavailableError,
    WalletUnavailableError,
)
from domain.models import DebitLookup, DebitResult, Refunded, Voided

logger = logging.getLogger(__name__)


class _RetryableWalletError(Exception):
    pass


class _UnexpectedWalletResponse(Exception):
    """A 4xx the contract doesn't allow — not retried, never read as an answer."""


# MA-153 FR-4 — Wallet's definite refund refusals (all 409s).
_REFUND_REFUSALS = {
    "DEBIT_NOT_FOUND": DebitNotFoundError,
    "REFUND_EXCEEDS_DEBIT": RefundExceedsDebitError,
    "ORDER_USER_MISMATCH": OrderUserMismatchError,
}


class HttpWalletClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def debit(
        self, user_id: str, order_id: str, amount_paise: int, correlation_id: str
    ) -> DebitResult:
        url = f"{self._base_url}/wallet/internal/debit"
        body = {
            "userId": user_id,
            "orderId": order_id,
            "amountPaise": amount_paise,
            "correlationId": correlation_id,
        }

        def _attempt() -> DebitResult:
            try:
                response = requests.post(
                    url,
                    json=body,
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    data = response.json()["data"]
                    # DEBITED carries balanceAfterPaise; INSUFFICIENT_BALANCE
                    # carries balancePaise instead (wallet/src/handlers/dto.py's
                    # serialize_debit_outcome) — WALLET_NOT_ACTIVE has neither.
                    balance = data.get("balanceAfterPaise", data.get("balancePaise"))
                    return DebitResult(status=data["status"], balance_after_paise=balance)
                except (ValueError, KeyError, TypeError) as exc:
                    raise _RetryableWalletError(
                        f"malformed 200 body from Wallet: {exc}"
                    ) from exc
            if response.status_code == 409 and _error_code(response) == "DEBIT_VOIDED":
                # MA-142 FR-3: a definite answer — the order was closed
                # without charge. Never retried.
                raise DebitVoidedError(
                    "Wallet refused the debit: the order was voided", {"orderId": order_id}
                )
            # 503 WALLET_PROVISIONING_PENDING included — retried like any
            # other 5xx, never treated as one of the three typed outcomes.
            raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "wallet_client.debit request failed",
                extra={
                    "correlationId": self._correlation_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableWalletError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableWalletError as exc:
            raise WalletUnavailableError(
                "Wallet debit request failed after retries", details={"cause": str(exc)}
            ) from exc

    def void_debit(self, user_id: str, order_id: str) -> Voided | DebitLookup:
        """MA-142 FR-2 — fence `order_id` before closing it without charge.
        `Voided`: no debit for it can ever commit. `DebitLookup`: it was
        already debited (`409 ALREADY_DEBITED`), so the caller must resume,
        not close. Idempotent, so safe to retry. Anything that isn't one of
        those two answers raises WalletUnavailableError: "couldn't ask" is
        never "not charged"."""
        url = f"{self._base_url}/wallet/internal/debits/{order_id}/void"

        def _attempt() -> Voided | DebitLookup:
            try:
                response = requests.post(
                    url,
                    json={"userId": user_id},
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc
            if response.status_code >= 500:
                raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")
            data = _data(response)
            if response.status_code == 200 and data.get("status") == "VOIDED":
                return _voided(data)
            if response.status_code == 409 and data.get("errorCode") == "ALREADY_DEBITED":
                return _debit_lookup(data)
            raise _UnexpectedWalletResponse(
                f"Wallet returned HTTP {response.status_code} ({data.get('errorCode')})"
            )

        return self._call_debits_route(_attempt, "void_debit", order_id)

    def get_debit(self, order_id: str) -> DebitLookup | Voided | None:
        """MA-142 FR-5 — `GET /wallet/internal/debits/{orderId}`, read-only.
        `None` (404 DEBIT_NOT_FOUND) only means "not debited *yet*": a
        debit whose response was lost can still commit, so no caller may
        close an order on it — only `void_debit` decides that."""
        url = f"{self._base_url}/wallet/internal/debits/{order_id}"

        def _attempt() -> DebitLookup | Voided | None:
            try:
                response = requests.get(
                    url,
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc
            if response.status_code >= 500:
                raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")
            data = _data(response)
            if response.status_code == 404 and data.get("errorCode") == "DEBIT_NOT_FOUND":
                return None
            if response.status_code != 200:
                raise _UnexpectedWalletResponse(
                    f"Wallet returned HTTP {response.status_code} ({data.get('errorCode')})"
                )
            return _voided(data) if data.get("status") == "VOIDED" else _debit_lookup(data)

        return self._call_debits_route(_attempt, "get_debit", order_id)

    def refund(
        self,
        user_id: str,
        order_id: str,
        refund_id: str,
        amount_paise: int,
        correlation_id: str,
    ) -> Refunded:
        """MA-153 FR-6 — `POST /wallet/internal/refunds`. Idempotent on
        (order_id, refund_id), so transport failures and 5xx are retried
        with the client's usual policy. Wallet's definite 409s raise their
        typed exception, never retried. Exhausted retries or a response
        outside the contract raise WalletUnavailableError."""
        url = f"{self._base_url}/wallet/internal/refunds"
        body = {
            "userId": user_id,
            "orderId": order_id,
            "refundId": refund_id,
            "amountPaise": amount_paise,
            "correlationId": correlation_id,
        }

        def _attempt() -> Refunded:
            try:
                response = requests.post(
                    url,
                    json=body,
                    timeout=self._timeout_seconds,
                    headers={
                        "x-request-id": self._correlation_id,
                        "X-Correlation-Id": correlation_id,
                    },
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc
            if response.status_code >= 500:
                raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")
            data = _data(response)
            if response.status_code == 200:
                return _refunded(data)
            refused = _REFUND_REFUSALS.get(data.get("errorCode"))
            if response.status_code == 409 and refused is not None:
                raise refused(
                    f"Wallet refused the refund: {data.get('errorCode')}",
                    {"orderId": order_id, "refundId": refund_id},
                )
            raise _UnexpectedWalletResponse(
                f"Wallet returned HTTP {response.status_code} ({data.get('errorCode')})"
            )

        return self._call_debits_route(_attempt, "refund", order_id)

    def _call_debits_route(self, attempt, operation: str, order_id: str):
        """Retry transport failures and 5xx with the client's usual policy;
        a response outside the contract is a bug, logged and not retried.
        Both end as WalletUnavailableError."""
        try:
            return call_with_retry(
                attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableWalletError,),
            )
        except _RetryableWalletError as exc:
            raise WalletUnavailableError(
                f"Wallet {operation} failed after retries", details={"cause": str(exc)}
            ) from exc
        except _UnexpectedWalletResponse as exc:
            logger.error(
                f"wallet_client.{operation} unexpected response",
                extra={"orderId": order_id, "error": str(exc)},
            )
            raise WalletUnavailableError(
                f"Wallet {operation} returned an unexpected response",
                details={"cause": str(exc)},
            ) from exc

    def get_balance(self, user_id: str) -> int:
        """MA-136 FR-3.7 — `GET /wallet/internal/balance` (MA-130 FR-3), in
        paise. Advisory only: checkout's pre-charge balance check. A
        wallet that isn't provisioned yet reads as 0 (Wallet's own
        CREATING-if-absent convention). Raises WalletBalanceUnavailableError
        after retries."""
        url = f"{self._base_url}/wallet/internal/balance"

        def _attempt() -> int:
            try:
                response = requests.get(
                    url,
                    params={"userId": user_id},
                    timeout=self._timeout_seconds,
                    headers={"x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableWalletError(str(exc)) from exc
            if response.status_code != 200:
                raise _RetryableWalletError(f"Wallet returned HTTP {response.status_code}")
            try:
                return int(response.json()["data"]["balancePaise"])
            except (ValueError, KeyError, TypeError) as exc:
                raise _RetryableWalletError(f"malformed 200 body from Wallet: {exc}") from exc

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableWalletError,),
            )
        except _RetryableWalletError as exc:
            raise WalletBalanceUnavailableError(
                "Wallet balance read failed after retries", details={"cause": str(exc)}
            ) from exc


def _error_code(response) -> str | None:
    try:
        return response.json()["data"].get("errorCode")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _data(response) -> dict:
    try:
        data = response.json()["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _UnexpectedWalletResponse(
            f"malformed HTTP {response.status_code} body from Wallet: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise _UnexpectedWalletResponse(f"malformed HTTP {response.status_code} body from Wallet")
    return data


def _debit_lookup(data: dict) -> DebitLookup:
    try:
        return DebitLookup(
            amount_paise=int(data["amountPaise"]),
            balance_after_paise=int(data["balanceAfterPaise"]),
            debited_at=datetime.fromisoformat(data["debitedAt"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise _UnexpectedWalletResponse(f"malformed debit body from Wallet: {exc}") from exc


def _refunded(data: dict) -> Refunded:
    try:
        return Refunded(
            amount_paise=int(data["amountPaise"]),
            balance_after_paise=int(data["balanceAfterPaise"]),
            refunded_at=datetime.fromisoformat(data["refundedAt"]),
            replayed=bool(data.get("replayed", False)),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise _UnexpectedWalletResponse(f"malformed refund body from Wallet: {exc}") from exc


def _voided(data: dict) -> Voided:
    try:
        return Voided(voided_at=datetime.fromisoformat(data["voidedAt"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise _UnexpectedWalletResponse(f"malformed void body from Wallet: {exc}") from exc
