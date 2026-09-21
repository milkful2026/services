"""Domain models. Plain dataclasses / enums only — no SQLAlchemy/FastAPI
types."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum

# India has no DST — a fixed UTC+5:30 offset is exact and needs no
# `tzdata` package (this repo has no existing IST-handling precedent to
# follow, and `zoneinfo.ZoneInfo` would need `tzdata` installed in the
# `python:3.11-slim` image for no benefit over a fixed offset here).
IST = timezone(timedelta(hours=5, minutes=30))


class ScheduleType(StrEnum):
    DAILY = "DAILY"
    ALTERNATE_DAYS = "ALTERNATE_DAYS"
    WEEKLY = "WEEKLY"
    CUSTOM_DAYS = "CUSTOM_DAYS"


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass
class Schedule:
    type: ScheduleType
    days_of_week: list[int] | None = None  # ISO weekday 1=Mon..7=Sun; WEEKLY/CUSTOM_DAYS only

    def to_dict(self) -> dict:
        return {"type": self.type.value, "daysOfWeek": self.days_of_week}

    @staticmethod
    def from_dict(data: dict) -> "Schedule":
        return Schedule(type=ScheduleType(data["type"]), days_of_week=data.get("daysOfWeek"))


@dataclass
class PendingEdit:
    quantity: int
    schedule: Schedule
    effective_from: date

    def to_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "schedule": self.schedule.to_dict(),
            "effectiveFrom": self.effective_from.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict) -> "PendingEdit":
        return PendingEdit(
            quantity=data["quantity"],
            schedule=Schedule.from_dict(data["schedule"]),
            effective_from=date.fromisoformat(data["effectiveFrom"]),
        )


@dataclass
class Subscription:
    id: str
    user_id: str
    product_id: str
    quantity: int
    schedule: Schedule
    slot_id: str
    status: SubscriptionStatus
    start_date: date
    pause_from: date | None = None
    pause_until: date | None = None
    pending_edit: PendingEdit | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
