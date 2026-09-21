"""Request/response DTOs + the `{requestId, status, data}` envelope
(services/README.md §5 — the shape the mobile app's shared ApiClient
unwraps for every service)."""

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from domain.exceptions import InvalidScheduleError
from domain.models import Schedule, ScheduleType


class ScheduleDto(BaseModel):
    type: str
    daysOfWeek: list[int] | None = None  # noqa: N815 — wire contract casing

    def to_domain(self) -> Schedule:
        try:
            schedule_type = ScheduleType(self.type)
        except ValueError as exc:
            raise InvalidScheduleError(f"Unknown schedule type {self.type!r}") from exc
        return Schedule(type=schedule_type, days_of_week=self.daysOfWeek)


class CreateSubscriptionRequest(BaseModel):
    productId: str  # noqa: N815
    quantity: int = Field(gt=0)
    schedule: ScheduleDto
    startDate: date  # noqa: N815
    slotId: str  # noqa: N815
    idempotencyKey: str  # noqa: N815


class PauseRequest(BaseModel):
    from_: date | None = Field(default=None, alias="from")
    until: date | None = None

    model_config = {"populate_by_name": True}


class SkipRequest(BaseModel):
    date: date


class EditRequest(BaseModel):
    quantity: int | None = Field(default=None, gt=0)
    schedule: ScheduleDto | None = None


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
