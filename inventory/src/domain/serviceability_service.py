"""Serviceability match rules (FR-1): active zone AND (pincode prefix OR
point-in-polygon); polygon preferred over pincode when lat/lng provided.

Cache-aside orchestration lives here (not in the handler) since "check
serviceability" — including whether a cached answer is still usable — is
business behavior, not transport plumbing. Cache key is `svc:{pincode}`
only (no lat/lng component), matching the spec exactly — a known
simplification: two requests for the same pincode with different lat/lng
can share a cached result until the 15-min TTL expires, which matters
only right at a polygon border.
"""

import re

from adapters.interfaces import ZoneCachePort, ZoneRepositoryPort
from domain.exceptions import InvalidPincodeError
from domain.models import ServiceabilityResult, Slot, Zone

_PINCODE_PATTERN = re.compile(r"^[1-9]\d{5}$")


class ServiceabilityService:
    def __init__(
        self,
        zone_repository: ZoneRepositoryPort,
        zone_cache: ZoneCachePort,
        cache_ttl_seconds: int = 900,
    ) -> None:
        self._zone_repository = zone_repository
        self._zone_cache = zone_cache
        self._cache_ttl_seconds = cache_ttl_seconds

    def check(self, pincode: str, lat: float | None, lng: float | None) -> ServiceabilityResult:
        if not _PINCODE_PATTERN.match(pincode):
            raise InvalidPincodeError(f"Invalid pincode format: {pincode!r}")

        cached = self._zone_cache.get(pincode)
        if cached is not None:
            return _result_from_dict(cached)

        zones = self._zone_repository.get_active_zones()
        result = self._match(pincode, lat, lng, zones)

        self._zone_cache.set(pincode, result_to_dict(result), self._cache_ttl_seconds)
        return result

    def _match(
        self, pincode: str, lat: float | None, lng: float | None, zones: list[Zone]
    ) -> ServiceabilityResult:
        active_zones = [z for z in zones if z.active]

        if lat is not None and lng is not None:
            polygon_match = _match_polygon(lat, lng, active_zones)
            if polygon_match is not None:
                return _serviceable_result(polygon_match)

        prefix_match = _match_prefix(pincode, active_zones)
        if prefix_match is not None:
            return _serviceable_result(prefix_match)

        return ServiceabilityResult(
            serviceable=False, message="We don't deliver to this area yet"
        )


def _match_polygon(lat: float, lng: float, zones: list[Zone]) -> Zone | None:
    from shapely.geometry import Point, Polygon

    point = Point(lng, lat)  # shapely uses (x, y) = (lng, lat)
    for zone in zones:
        if not zone.polygon or len(zone.polygon) < 3:
            continue
        # zone.polygon is [(lat, lng), ...] — convert to shapely's (x, y)
        ring = [(p_lng, p_lat) for (p_lat, p_lng) in zone.polygon]
        if Polygon(ring).contains(point):
            return zone
    return None


def _match_prefix(pincode: str, zones: list[Zone]) -> Zone | None:
    for zone in zones:
        for prefix in zone.pincode_prefixes:
            if pincode.startswith(prefix):
                return zone
    return None


def _serviceable_result(zone: Zone) -> ServiceabilityResult:
    return ServiceabilityResult(
        serviceable=True, zone_id=zone.id, zone_name=zone.name, slots=zone.slots
    )


def result_to_dict(result: ServiceabilityResult) -> dict:
    """The canonical `ServiceabilityResult` <-> dict shape — shared by the
    cache-aside serialization here and by `handlers/dto.serialize_result`,
    so the cached representation and the API response can't drift apart."""
    return {
        "serviceable": result.serviceable,
        "zoneId": result.zone_id,
        "zoneName": result.zone_name,
        "slots": [{"id": s.id, "label": s.label} for s in result.slots],
        "message": result.message,
        "waitlistAvailable": result.waitlist_available,
    }


def _result_from_dict(data: dict) -> ServiceabilityResult:
    return ServiceabilityResult(
        serviceable=data["serviceable"],
        zone_id=data.get("zoneId"),
        zone_name=data.get("zoneName"),
        slots=[Slot(id=s["id"], label=s["label"]) for s in data.get("slots", [])],
        message=data.get("message"),
        waitlist_available=data.get("waitlistAvailable", False),
    )
