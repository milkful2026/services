"""Typed domain exceptions. Every exception carries a stable `error_code`
and the HTTP status handlers map it to — never a raw traceback."""

from typing import Any


class WalletError(Exception):
    error_code: str = "WALLET_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class WalletNotFoundError(WalletError):
    error_code = "WALLET_NOT_FOUND"
    http_status = 404


class InvalidCursorError(WalletError):
    error_code = "INVALID_CURSOR"
    http_status = 400


class ServiceUnavailableError(WalletError):
    """DB unreachable — fail closed (never silently return a stale/empty
    balance or ledger)."""

    error_code = "SERVICE_UNAVAILABLE"
    http_status = 503


class RetryableConsumerError(WalletError):
    """Raised by the events consumer when a message cannot be processed
    *now* but must not be dropped (e.g. a recharge PaymentConfirmed
    arrives before the wallet's UserRegistered create was processed). The
    consumer leaves the SQS message in-queue → redelivery → DLQ after the
    redrive count, with an alarm. Never acked-and-dropped."""

    error_code = "CONSUMER_RETRYABLE"
    http_status = 503


class OrderUserMismatchError(WalletError):
    """A replayed `debit_for_order` for an already-debited `orderId`
    resolves to a wallet different from the one just locked. `orderId` is
    server-generated and globally unique, so this should be impossible —
    checked defensively rather than trusting it, the only place this can
    run since it's entirely local data (no Order Service call needed)."""

    error_code = "ORDER_USER_MISMATCH"
    http_status = 400


class InvalidAmountError(WalletError):
    error_code = "INVALID_AMOUNT"
    http_status = 400


class WalletProvisioningPendingError(WalletError):
    """Raised by the synchronous `debit_for_order` path (MA-25/MA-130)
    when no wallet row exists yet for this user — the same
    UserRegistered-before-wallet-exists race `credit_recharge` already
    treats as retryable via `RetryableConsumerError`, but this call comes
    from Order Service over HTTP, not an SQS consumer, so it must surface
    as a retryable HTTP status (503) rather than a queue-level retry.
    Deliberately distinct from `WALLET_NOT_ACTIVE` (a settled,
    non-retryable state, returned as a normal 200 `DebitOutcome` instead)
    — folding the two together would let a subscription's first-ever
    order (created moments after registration) permanently fail instead
    of retrying once provisioning catches up."""

    error_code = "WALLET_PROVISIONING_PENDING"
    http_status = 503
