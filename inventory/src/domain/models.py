"""Domain models. Plain dataclasses only — no SQLAlchemy/FastAPI types."""

from dataclasses import dataclass, field


@dataclass
class Slot:
    id: str
    label: str


@dataclass
class Zone:
    id: str
    name: str
    active: bool
    pincode_prefixes: list[str]
    polygon: list[tuple[float, float]] | None  # [(lat, lng), ...], closed ring; None if unset
    slots: list[Slot] = field(default_factory=list)


@dataclass
class ServiceabilityResult:
    serviceable: bool
    zone_id: str | None = None
    zone_name: str | None = None
    slots: list[Slot] = field(default_factory=list)
    message: str | None = None
    waitlist_available: bool = False  # always False — waitlist is phase 2 (spec G2)
