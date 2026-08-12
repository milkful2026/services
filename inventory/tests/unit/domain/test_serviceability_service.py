import pytest

from domain.exceptions import InvalidPincodeError
from domain.models import Slot, Zone
from domain.serviceability_service import ServiceabilityService


class FakeZoneRepository:
    def __init__(self, zones: list[Zone]):
        self.zones = zones
        self.call_count = 0

    def get_active_zones(self) -> list[Zone]:
        self.call_count += 1
        return self.zones


class FakeZoneCache:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple] = []

    def get(self, pincode: str):
        self.get_calls.append(pincode)
        return self.store.get(pincode)

    def set(self, pincode: str, result: dict, ttl_seconds: int) -> None:
        self.set_calls.append((pincode, ttl_seconds))
        self.store[pincode] = result

    def invalidate(self, pincode: str) -> None:
        self.store.pop(pincode, None)


BLR_CENTRAL = Zone(
    id="blr-central",
    name="Bangalore Central",
    active=True,
    pincode_prefixes=["5600"],
    polygon=None,
    slots=[Slot(id="morning-6-8", label="Morning 6-8 AM")],
)

# A small square polygon around (12.97, 77.59), roughly Bangalore Central.
BLR_POLYGON_ZONE = Zone(
    id="blr-polygon",
    name="Bangalore Polygon Zone",
    active=True,
    pincode_prefixes=["9999"],  # deliberately non-matching prefix
    polygon=[(12.90, 77.50), (12.90, 77.70), (13.05, 77.70), (13.05, 77.50)],
    slots=[Slot(id="evening-6-8", label="Evening 6-8 PM")],
)

INACTIVE_ZONE = Zone(
    id="inactive-zone", name="Inactive", active=False, pincode_prefixes=["5600"], polygon=None
)


@pytest.fixture
def cache():
    return FakeZoneCache()


def test_serviceable_by_pincode_prefix(cache):
    repo = FakeZoneRepository([BLR_CENTRAL])
    service = ServiceabilityService(repo, cache)

    result = service.check("560001", None, None)

    assert result.serviceable is True
    assert result.zone_id == "blr-central"
    assert result.slots[0].id == "morning-6-8"


def test_not_serviceable_when_no_zone_matches(cache):
    repo = FakeZoneRepository([BLR_CENTRAL])
    service = ServiceabilityService(repo, cache)

    result = service.check("110001", None, None)

    assert result.serviceable is False
    assert result.zone_id is None
    assert result.message == "We don't deliver to this area yet"


def test_invalid_pincode_format_raises(cache):
    repo = FakeZoneRepository([])
    service = ServiceabilityService(repo, cache)

    with pytest.raises(InvalidPincodeError):
        service.check("12AB56", None, None)


def test_inactive_zone_is_excluded(cache):
    repo = FakeZoneRepository([INACTIVE_ZONE])
    service = ServiceabilityService(repo, cache)

    result = service.check("560001", None, None)

    assert result.serviceable is False


def test_polygon_match_preferred_over_pincode_when_lat_lng_given(cache):
    repo = FakeZoneRepository([BLR_CENTRAL, BLR_POLYGON_ZONE])
    service = ServiceabilityService(repo, cache)

    # Inside the polygon, and pincode 560001 would otherwise match BLR_CENTRAL.
    result = service.check("560001", 12.97, 77.59)

    assert result.serviceable is True
    assert result.zone_id == "blr-polygon"


def test_falls_back_to_pincode_when_point_outside_all_polygons(cache):
    repo = FakeZoneRepository([BLR_CENTRAL, BLR_POLYGON_ZONE])
    service = ServiceabilityService(repo, cache)

    # Far outside the polygon, but pincode still matches BLR_CENTRAL.
    result = service.check("560001", 1.0, 1.0)

    assert result.serviceable is True
    assert result.zone_id == "blr-central"


def test_cache_hit_does_not_call_repository(cache):
    repo = FakeZoneRepository([BLR_CENTRAL])
    service = ServiceabilityService(repo, cache)

    service.check("560001", None, None)
    assert repo.call_count == 1

    service.check("560001", None, None)
    assert repo.call_count == 1  # second call served entirely from cache


def test_cache_miss_writes_result_with_configured_ttl(cache):
    repo = FakeZoneRepository([BLR_CENTRAL])
    service = ServiceabilityService(repo, cache, cache_ttl_seconds=900)

    service.check("560001", None, None)

    assert cache.set_calls == [("560001", 900)]
