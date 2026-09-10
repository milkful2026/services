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
