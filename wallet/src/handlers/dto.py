"""Response envelope + serialization helpers. Fixed `{requestId, status,
data}` shape per services/README.md §5 — the shape the mobile app's
shared ApiClient unwraps for every service."""

import uuid
from typing import Any

from domain.models import LedgerEntry, TransactionsPage
from domain.wallet_service import render_description


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


def success_envelope(data: dict[str, Any] | list[Any]) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def error_envelope(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "status": "error",
        "data": {"errorCode": error_code, "message": message, **(details or {})},
    }
