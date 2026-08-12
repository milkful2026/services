import pytest
from sqlalchemy.exc import OperationalError

from adapters.user_repository import SqlAlchemyUserRepository, zone_slots_table
from domain.exceptions import ExternalServiceUnavailableError
from domain.models import Address, Consent


@pytest.fixture
def repository(sqlite_engine):
    return SqlAlchemyUserRepository(engine=sqlite_engine)


def _address(**overrides) -> Address:
    defaults = dict(
        lines=["12 MG Road"], city="Bangalore", state="Karnataka", pincode="560001",
        lat=12.9716, lng=77.5946, is_default=True,
    )
    defaults.update(overrides)
    return Address(**defaults)


def _consents() -> list[Consent]:
    return [
        Consent(type="TERMS", version="2026-01", accepted_at="2026-07-20T10:00:00Z"),
        Consent(type="PRIVACY", version="2026-01", accepted_at="2026-07-20T10:00:00Z"),
    ]


def test_register_creates_user_address_consent_and_outbox_row(repository, sqlite_engine):
    result = repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id="morning-6-8", consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={"cognitoSub": "sub-1"},
    )

    assert result.is_new_user is True
    assert result.user_id
    assert result.default_address_id

    from adapters.user_repository import outbox_events_table, users_table

    with sqlite_engine.connect() as conn:
        user_row = conn.execute(users_table.select()).fetchone()
        outbox_row = conn.execute(outbox_events_table.select()).fetchone()

    assert user_row.cognito_sub == "sub-1"
    assert outbox_row.type == "UserRegistered"
    assert outbox_row.published_at is None


def test_register_is_idempotent_on_duplicate_cognito_sub(repository):
    first = repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id=None, consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={},
    )

    second = repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id=None, consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={},
    )

    assert second.is_new_user is False
    assert second.user_id == first.user_id
    assert second.default_address_id == first.default_address_id


def test_register_duplicate_does_not_write_a_second_outbox_row(repository, sqlite_engine):
    from adapters.user_repository import outbox_events_table

    repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id=None, consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={},
    )
    repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id=None, consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={},
    )

    with sqlite_engine.connect() as conn:
        count = len(conn.execute(outbox_events_table.select()).fetchall())
    assert count == 1


def test_register_fails_closed_on_db_error(repository, sqlite_engine, monkeypatch):
    def _raise(*args, **kwargs):
        raise OperationalError("begin", {}, Exception("db down"))

    monkeypatch.setattr(sqlite_engine, "begin", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        repository.register(
            cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
            addresses=[_address()], preferred_slot_id=None, consents=_consents(),
            outbox_event_type="UserRegistered", outbox_payload={},
        )


def test_get_delivery_slots_returns_active_slots_for_zone(repository, sqlite_engine):
    with sqlite_engine.begin() as conn:
        conn.execute(
            zone_slots_table.insert().values(
                zone_id="blr-central", slot_id="morning-6-8", label="Morning 6-8 AM", active=True
            )
        )
        conn.execute(
            zone_slots_table.insert().values(
                zone_id="blr-central", slot_id="old-slot", label="Retired", active=False
            )
        )
        conn.execute(
            zone_slots_table.insert().values(
                zone_id="other-zone", slot_id="evening-6-8", label="Evening", active=True
            )
        )

    slots = repository.get_delivery_slots("blr-central")

    assert len(slots) == 1
    assert slots[0].id == "morning-6-8"


def test_get_unpublished_outbox_events_and_mark_published(repository, sqlite_engine):
    repository.register(
        cognito_sub="sub-1", mobile="+919876543210", name="Priya", email=None,
        addresses=[_address()], preferred_slot_id=None, consents=_consents(),
        outbox_event_type="UserRegistered", outbox_payload={"cognitoSub": "sub-1"},
    )

    unpublished = repository.get_unpublished_outbox_events(limit=10)
    assert len(unpublished) == 1
    assert unpublished[0]["type"] == "UserRegistered"

    repository.mark_outbox_published(unpublished[0]["id"])

    assert repository.get_unpublished_outbox_events(limit=10) == []
