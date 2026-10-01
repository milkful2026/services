"""Request/response DTOs for the admin customer-status endpoints (MA-139
§4). Response envelopes reuse the shared helpers in handlers/dto.py."""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from domain.models import BulkStatusResult, CustomerAccount, CustomerPage, UserStatusHistoryEntry


class SuspendRequest(BaseModel):
    reason: str
    until: date


class DeactivateRequest(BaseModel):
    reason: str


class ReactivateRequest(BaseModel):
    reason: str | None = None


class BulkStatusRequest(BaseModel):
    customer_ids: list[str] = Field(alias="customerIds")
    action: str
    reason: str | None = None
    until: date | None = None

    model_config = {"populate_by_name": True}


def serialize_customer_account(account: CustomerAccount) -> dict[str, Any]:
    """Spec §4 FR-1's list-row shape. `lastStatusChangeAt` comes from
    account.last_status_change_at (the most recent
    user_status_history.created_at — see CustomerAccount's own
    docstring), never status_effective_from."""
    return {
        "id": account.id,
        "name": account.name,
        "mobile": account.mobile,
        "email": account.email,
        "accountType": account.account_type,
        "status": account.status,
        "statusReason": account.status_reason,
        "lastStatusChangeAt": (
            account.last_status_change_at.isoformat() if account.last_status_change_at else None
        ),
    }


def serialize_status_history_entry(entry: UserStatusHistoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "previousStatus": entry.previous_status,
        "newStatus": entry.new_status,
        "reason": entry.reason,
        "effectiveFrom": entry.effective_from.isoformat() if entry.effective_from else None,
        "actorAdminId": entry.actor_admin_id,
        "createdAt": entry.created_at.isoformat() if entry.created_at else None,
    }


def serialize_customer_detail(account: CustomerAccount) -> dict[str, Any]:
    """Spec §4 FR-2 — the account's current profile plus its full
    user_status_history, newest first (the repository already orders it
    that way)."""
    detail = serialize_customer_account(account)
    detail["suspendedUntil"] = account.suspended_until.isoformat() if account.suspended_until else None
    detail["statusHistory"] = [serialize_status_history_entry(e) for e in account.status_history]
    return detail


def serialize_customer_page(page: CustomerPage) -> dict[str, Any]:
    return {
        "items": [serialize_customer_account(a) for a in page.items],
        "total": page.total,
        "page": page.page,
        "pageSize": page.page_size,
    }


def serialize_bulk_results(results: list[BulkStatusResult]) -> list[dict[str, Any]]:
    return [
        {"customerId": r.customer_id, "success": r.success, "errorCode": r.error_code}
        for r in results
    ]
