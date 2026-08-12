import pytest
from sqlalchemy.exc import OperationalError

from adapters.zone_repository import SqlAlchemyZoneRepository
from domain.exceptions import ServiceUnavailableError
from tests.conftest import seed_zone


@pytest.fixture
def repository(sqlite_engine):
    return SqlAlchemyZoneRepository(engine=sqlite_engine)


def test_get_active_zones_returns_seeded_zone(sqlite_engine, repository):
    seed_zone(sqlite_engine)

    zones = repository.get_active_zones()

    assert len(zones) == 1
    zone = zones[0]
    assert zone.id == "blr-central"
    assert zone.pincode_prefixes == ["5600"]
    assert zone.slots[0].id == "morning-6-8"
    assert zone.polygon is None


def test_get_active_zones_excludes_inactive(sqlite_engine, repository):
    seed_zone(sqlite_engine, id="active-zone", active=True)
    seed_zone(sqlite_engine, id="inactive-zone", active=False, pincode_prefixes=["1100"])

    zones = repository.get_active_zones()

    assert {z.id for z in zones} == {"active-zone"}


def test_get_active_zones_round_trips_polygon(sqlite_engine, repository):
    polygon = [[12.90, 77.50], [12.90, 77.70], [13.05, 77.70], [13.05, 77.50]]
    seed_zone(sqlite_engine, id="polygon-zone", polygon=polygon, pincode_prefixes=[])

    zones = repository.get_active_zones()

    assert zones[0].polygon == [(12.90, 77.50), (12.90, 77.70), (13.05, 77.70), (13.05, 77.50)]


def test_get_active_zones_empty_when_no_rows(repository):
    assert repository.get_active_zones() == []


def test_get_active_zones_returns_deterministic_order(sqlite_engine, repository):
    # Insert in an order different from id sort order — without ORDER BY,
    # a match that picks "the first zone returned" for overlapping zones
    # would be non-deterministic across requests/deploys.
    seed_zone(sqlite_engine, id="zone-c", pincode_prefixes=["5600"])
    seed_zone(sqlite_engine, id="zone-a", pincode_prefixes=["5600"])
    seed_zone(sqlite_engine, id="zone-b", pincode_prefixes=["5600"])

    zones = repository.get_active_zones()

    assert [z.id for z in zones] == ["zone-a", "zone-b", "zone-c"]


def test_get_active_zones_fails_closed_on_db_error(sqlite_engine, repository, monkeypatch):
    def _raise(*args, **kwargs):
        raise OperationalError("connect", {}, Exception("connection refused"))

    monkeypatch.setattr(sqlite_engine, "connect", _raise)

    with pytest.raises(ServiceUnavailableError):
        repository.get_active_zones()
