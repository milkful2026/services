"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on SQLAlchemy/redis/boto3 directly."""

from typing import Protocol

from domain.models import Zone


class ZoneRepositoryPort(Protocol):
    def get_active_zones(self) -> list[Zone]:
        """Raises ServiceUnavailableError on any DB failure — fail closed,
        per spec NFR (never silently return an empty/stale list)."""
        ...


class ZoneCachePort(Protocol):
    def get(self, pincode: str) -> dict | None: ...
    def set(self, pincode: str, result: dict, ttl_seconds: int) -> None: ...
    def invalidate(self, pincode: str) -> None: ...
