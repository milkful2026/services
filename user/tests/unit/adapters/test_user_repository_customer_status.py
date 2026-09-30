"""MA-139 repository tests — list/detail/history/status-update/sweep
against the real SQLAlchemy Core table definitions on SQLite (same
fidelity-gap posture as test_user_repository.py)."""

import pytest

from adapters.user_repository import SqlAlchemyUserRepository
from domain.exceptions import CustomerNotFoundError
from domain.models import Address, Consent, CustomerStatus


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


def _register(repository, cognito_sub="sub-1", name="Priya", mobile="+919876543210", email=None) -> str:
    result = repository.register(
        cognito_sub=cognito_sub,
        mobile=mobile,
        name=name,
        email=email,
        addresses=[_address()],
        preferred_slot_id=None,
        consents=[Consent(type="TERMS", accepted_at="2026-07-20T10:00:00Z")],
        outbox_event_type="UserRegistered",
        outbox_payload={"userId": cognito_sub},
    )
    return result.user_id


def test_new_user_defaults_to_active_status(repository):
    user_id = _register(repository)
    account = repository.get_customer_by_id(user_id)
    assert account.status == CustomerStatus.ACTIVE.value
    assert account.last_status_change_at is None  # never had a status change


def test_get_customer_by_id_returns_none_for_unknown_id(repository):
    assert repository.get_customer_by_id("does-not-exist") is None


def test_update_customer_status_writes_users_history_and_outbox_in_one_transaction(
    repository, sqlite_engine
):
    user_id = _register(repository)

    updated = repository.update_customer_status(
        user_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="fraud",
        status_effective_from=__import__("datetime").date(2026, 9, 30),
        history_effective_from=__import__("datetime").date(2026, 9, 30),
        suspended_until=__import__("datetime").date(2026, 10, 15),
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={"userId": user_id, "newStatus": "Suspended"},
    )

    assert updated.status == CustomerStatus.SUSPENDED.value
    assert updated.status_reason == "fraud"
    assert updated.suspended_until == __import__("datetime").date(2026, 10, 15)

    from adapters.user_repository import outbox_events_table, user_status_history_table

    with sqlite_engine.connect() as conn:
        history_rows = conn.execute(user_status_history_table.select()).fetchall()
        outbox_rows = conn.execute(outbox_events_table.select()).fetchall()

    assert len(history_rows) == 1
    assert history_rows[0].previous_status == CustomerStatus.ACTIVE.value
    assert history_rows[0].new_status == CustomerStatus.SUSPENDED.value
    assert history_rows[0].actor_admin_id == "admin-1"

    status_events = [r for r in outbox_rows if r.type == "user.status.changed"]
    assert len(status_events) == 1
    assert status_events[0].payload["newStatus"] == "Suspended"


def test_update_customer_status_raises_not_found_for_unknown_customer(repository):
    with pytest.raises(CustomerNotFoundError):
        repository.update_customer_status(
            "missing",
            new_status=CustomerStatus.SUSPENDED.value,
            status_reason="reason",
            status_effective_from=None,
            history_effective_from=None,
            suspended_until=None,
            actor_admin_id="admin-1",
            outbox_event_type="user.status.changed",
            outbox_payload={},
        )


def test_get_status_history_is_newest_first(repository, sqlite_engine):
    """SQLite's CURRENT_TIMESTAMP (what `func.now()` compiles to there)
    only has 1-second resolution — unlike Postgres's microsecond-precision
    `now()`, two history rows inserted milliseconds apart under the test
    DB can otherwise tie. Backdating the first row's created_at directly
    keeps this test fast and deterministic without relying on a real
    wall-clock sleep (same documented SQLite-vs-Postgres fidelity gap
    this module's own docstring already calls out)."""
    import datetime

    from adapters.user_repository import user_status_history_table

    user_id = _register(repository)
    repository.update_customer_status(
        user_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="first",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=None,
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )
    with sqlite_engine.begin() as conn:
        conn.execute(
            user_status_history_table.update()
            .where(user_status_history_table.c.reason == "first")
            .values(created_at=datetime.datetime(2020, 1, 1))
        )
    repository.update_customer_status(
        user_id,
        new_status=CustomerStatus.ACTIVE.value,
        status_reason="second",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=None,
        actor_admin_id="admin-2",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )

    history = repository.get_status_history(user_id)
    assert len(history) == 2
    assert history[0].reason == "second"  # newest first
    assert history[1].reason == "first"


def test_list_customers_filters_by_status(repository):
    active_id = _register(repository, cognito_sub="sub-active", mobile="+910000000001")
    suspended_id = _register(repository, cognito_sub="sub-suspended", mobile="+910000000002")
    repository.update_customer_status(
        suspended_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="r",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=None,
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )

    page = repository.list_customers(CustomerStatus.SUSPENDED.value, None, 1, 20)
    assert [a.id for a in page.items] == [suspended_id]
    assert page.total == 1

    all_page = repository.list_customers(None, None, 1, 20)
    assert {a.id for a in all_page.items} == {active_id, suspended_id}


def test_list_customers_search_matches_name_mobile_or_email(repository):
    user_id = _register(repository, name="Priya Sharma", mobile="+919876543210", email="priya@x.com")
    _register(repository, cognito_sub="sub-2", name="Rahul", mobile="+911111111111", email="rahul@x.com")

    by_name = repository.list_customers(None, "priya", 1, 20)
    assert [a.id for a in by_name.items] == [user_id]

    by_mobile = repository.list_customers(None, "9876543210", 1, 20)
    assert [a.id for a in by_mobile.items] == [user_id]

    by_email = repository.list_customers(None, "priya@x.com", 1, 20)
    assert [a.id for a in by_email.items] == [user_id]


def test_list_customers_last_status_change_at_reflects_most_recent_history_row(repository):
    user_id = _register(repository)
    assert repository.list_customers(None, None, 1, 20).items[0].last_status_change_at is None

    repository.update_customer_status(
        user_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="r",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=None,
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )
    page = repository.list_customers(None, None, 1, 20)
    assert page.items[0].last_status_change_at is not None


def test_list_expired_suspensions_only_returns_past_due(repository):
    import datetime

    expired_id = _register(repository, cognito_sub="sub-expired", mobile="+910000000003")
    future_id = _register(repository, cognito_sub="sub-future", mobile="+910000000004")
    repository.update_customer_status(
        expired_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="r",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=datetime.date(2026, 9, 1),
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )
    repository.update_customer_status(
        future_id,
        new_status=CustomerStatus.SUSPENDED.value,
        status_reason="r",
        status_effective_from=None,
        history_effective_from=None,
        suspended_until=datetime.date(2026, 12, 1),
        actor_admin_id="admin-1",
        outbox_event_type="user.status.changed",
        outbox_payload={},
    )

    expired = repository.list_expired_suspensions(datetime.date(2026, 9, 30))
    assert [a.id for a in expired] == [expired_id]


def test_new_user_defaults_to_cognito_sync_not_pending(repository):
    user_id = _register(repository)
    account = repository.get_customer_by_id(user_id)
    assert account.cognito_sync_pending is False


def test_set_cognito_sync_pending_round_trips(repository):
    user_id = _register(repository)

    repository.set_cognito_sync_pending(user_id, True)
    assert repository.get_customer_by_id(user_id).cognito_sync_pending is True

    repository.set_cognito_sync_pending(user_id, False)
    assert repository.get_customer_by_id(user_id).cognito_sync_pending is False


def test_list_cognito_sync_pending_only_returns_flagged_accounts(repository):
    pending_id = _register(repository, cognito_sub="sub-pending", mobile="+910000000005")
    clean_id = _register(repository, cognito_sub="sub-clean", mobile="+910000000006")
    repository.set_cognito_sync_pending(pending_id, True)

    pending = repository.list_cognito_sync_pending()

    assert [a.id for a in pending] == [pending_id]
    assert clean_id not in [a.id for a in pending]
