import uuid

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError

from adapters.user_repository import SqlAlchemyUserRepository, users_table, zone_slots_table
from domain.exceptions import ExternalServiceUnavailableError
from domain.models import Address, Consent


@pytest.fixture
def repository(sqlite_engine):
    return SqlAlchemyUserRepository(engine=sqlite_engine)


def _address(**overrides) -> Address:
    defaults = dict(
        lines=["12 MG Road"],
        city="Bangalore",
        state="Karnataka",
        pincode="560001",
        lat=12.9716,
        lng=77.5946,
        is_default=True,
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
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id="morning-6-8",
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={"cognitoSub": "sub-1"},
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
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    second = repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    assert second.is_new_user is False
    assert second.user_id == first.user_id
    assert second.default_address_id == first.default_address_id


def test_register_duplicate_does_not_write_a_second_outbox_row(repository, sqlite_engine):
    from adapters.user_repository import outbox_events_table

    repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )
    repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    with sqlite_engine.connect() as conn:
        count = len(conn.execute(outbox_events_table.select()).fetchall())
    assert count == 1


def test_register_race_returns_existing_row_instead_of_raising(
    repository, sqlite_engine, monkeypatch
):
    # Simulates two concurrent register() calls for the same cognito_sub:
    # the row below is pre-seeded as if the other transaction already
    # committed, and this call's own existing-check SELECT is forced to
    # miss it (as if that SELECT ran just before the other commit) — so
    # it proceeds to INSERT and collides on the unique constraint.
    winning_user_id = str(uuid.uuid4())
    with sqlite_engine.begin() as conn:
        conn.execute(
            users_table.insert().values(
                id=winning_user_id,
                cognito_sub="sub-1",
                name="Priya",
                mobile="+919876543210",
                email=None,
                preferred_slot_id=None,
            )
        )

    real_execute = Connection.execute
    seen_select = {"done": False}

    def _patched_execute(self, statement, *args, **kwargs):
        if not seen_select["done"] and getattr(statement, "is_select", False):
            seen_select["done"] = True

            class _EmptyResult:
                def fetchone(self):
                    return None

            return _EmptyResult()
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", _patched_execute)

    result = repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    assert result.is_new_user is False
    assert result.user_id == winning_user_id


def test_get_by_cognito_sub_returns_none_when_absent(repository):
    assert repository.get_by_cognito_sub("does-not-exist") is None


def test_get_by_cognito_sub_finds_registered_user(repository):
    registered = repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    found = repository.get_by_cognito_sub("sub-1")

    assert found is not None
    assert found.user_id == registered.user_id
    assert found.default_address_id == registered.default_address_id
    assert found.is_new_user is False


def test_get_profile_by_sub_returns_none_when_absent(repository):
    assert repository.get_profile_by_sub("does-not-exist") is None


def test_get_profile_by_sub_returns_profile_defaulting_to_b2c(repository):
    registered = repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya Sharma",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    profile = repository.get_profile_by_sub("sub-1")

    assert profile is not None
    assert profile.user_id == registered.user_id
    assert profile.name == "Priya Sharma"
    assert profile.mobile == "+919876543210"
    assert profile.account_type == "B2C"
    assert profile.default_address_id == registered.default_address_id
    assert profile.default_address_state == "Karnataka"


def test_get_profile_by_sub_returns_null_state_when_no_default_address(repository):
    registered = repository.register(
        cognito_sub="sub-2",
        mobile="+919876543211",
        name="Amit Rao",
        email=None,
        addresses=[_address(is_default=False)],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={},
    )

    profile = repository.get_profile_by_sub("sub-2")

    assert profile is not None
    assert registered.default_address_id == ""
    assert profile.default_address_id == ""
    assert profile.default_address_state is None


def test_account_type_check_constraint_rejects_invalid_value(repository, sqlite_engine):
    # Guards the CHECK (account_type IN ('B2C','B2B')) constraint itself
    # (migrations/0002_add_account_type.sql / the CheckConstraint on
    # users_table) — a typo in the IN-list or a migration that loosens
    # it must fail a test, not ship silently.
    with pytest.raises(IntegrityError):
        with sqlite_engine.begin() as conn:
            conn.execute(
                users_table.insert().values(
                    id=str(uuid.uuid4()),
                    cognito_sub="sub-invalid",
                    name="Priya",
                    mobile="+919876543210",
                    email=None,
                    preferred_slot_id=None,
                    account_type="B2X",
                )
            )


def test_get_profile_by_sub_fails_closed_on_db_error(repository, sqlite_engine, monkeypatch):
    def _raise(*args, **kwargs):
        raise OperationalError("connect", {}, Exception("db down"))

    monkeypatch.setattr(sqlite_engine, "connect", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        repository.get_profile_by_sub("sub-1")


def test_register_fails_closed_on_db_error(repository, sqlite_engine, monkeypatch):
    def _raise(*args, **kwargs):
        raise OperationalError("begin", {}, Exception("db down"))

    monkeypatch.setattr(sqlite_engine, "begin", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        repository.register(
            cognito_sub="sub-1",
            mobile="+919876543210",
            name="Priya",
            email=None,
            addresses=[_address()],
            preferred_slot_id=None,
            consents=_consents(),
            outbox_event_type="UserRegistered",
            outbox_payload={},
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


def test_get_unpublished_outbox_events_returns_oldest_first(repository, sqlite_engine):
    from datetime import UTC, datetime

    from adapters.user_repository import outbox_events_table

    # Inserted in reverse chronological order — without ORDER BY, scan
    # order (not insertion time) would decide which events get processed
    # first once the batch size is smaller than the backlog.
    with sqlite_engine.begin() as conn:
        conn.execute(
            outbox_events_table.insert().values(
                id="evt-newer",
                aggregate_id="user-1",
                type="UserRegistered",
                payload={},
                published_at=None,
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        conn.execute(
            outbox_events_table.insert().values(
                id="evt-older",
                aggregate_id="user-1",
                type="UserRegistered",
                payload={},
                published_at=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    events = repository.get_unpublished_outbox_events(limit=10)

    assert [e["id"] for e in events] == ["evt-older", "evt-newer"]


def test_get_unpublished_outbox_events_and_mark_published(repository, sqlite_engine):
    repository.register(
        cognito_sub="sub-1",
        mobile="+919876543210",
        name="Priya",
        email=None,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=_consents(),
        outbox_event_type="UserRegistered",
        outbox_payload={"cognitoSub": "sub-1"},
    )

    unpublished = repository.get_unpublished_outbox_events(limit=10)
    assert len(unpublished) == 1
    assert unpublished[0]["type"] == "UserRegistered"

    repository.mark_outbox_published(unpublished[0]["id"])

    assert repository.get_unpublished_outbox_events(limit=10) == []
