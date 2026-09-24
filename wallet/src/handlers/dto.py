"""Response envelope + serialization helpers. Fixed `{requestId, status,
data}` shape per services/README.md §5 — the shape the mobile app's
shared ApiClient unwraps for every service."""

from typing import Any

from pydantic import BaseModel, Field
from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401

from domain.models import DebitOutcome, DebitResult, LedgerEntry, TransactionsPage
from domain.wallet_service import render_description


class DebitRequest(BaseModel):
    userId: str  # noqa: N815 — wire contract casing
    orderId: str  # noqa: N815
    amountPaise: int = Field(gt=0)  # noqa: N815
    correlationId: str | None = None  # noqa: N815


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_entry(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "id": f"led_{entry.id}",
        "type": entry.type.value,
        "amountPaise": entry.amount_paise,
        "balanceAfterPaise": entry.balance_after_paise,
        "ref": entry.ref,
        "description": render_description(entry),
        "createdAt": _iso(entry.created_at),
    }


def serialize_transactions(page: TransactionsPage) -> dict[str, Any]:
    return {
        "items": [serialize_entry(e) for e in page.items],
        "nextCursor": page.next_cursor,
    }


def serialize_debit_outcome(outcome: DebitOutcome) -> dict[str, Any]:
    """MA-130 §6 response shapes — all three results are 200s, branched
    on `status`, never an HTTP error (Order Service's wallet_client_adapter
    must not treat any of them as a failure)."""
    if outcome.result == DebitResult.DEBITED:
        return {"status": DebitResult.DEBITED.value, "balanceAfterPaise": outcome.balance_paise}
    if outcome.result == DebitResult.INSUFFICIENT_BALANCE:
        return {
            "status": DebitResult.INSUFFICIENT_BALANCE.value,
            "balancePaise": outcome.balance_paise,
            "requiredPaise": outcome.required_paise,
        }
    return {"status": DebitResult.WALLET_NOT_ACTIVE.value}
