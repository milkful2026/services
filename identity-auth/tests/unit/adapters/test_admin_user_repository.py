import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from adapters.admin_user_repository import SqlAlchemyAdminUserRepository, admin_user_table
from domain.admin_exceptions import AdminEmailExistsError
from domain.admin_models import AdminRole, AdminStatus, AdminUser
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def repository(admin_sqlite_engine):
    return SqlAlchemyAdminUserRepository(engine=admin_sqlite_engine)


def _admin(**overrides) -> AdminUser:
    defaults = dict(
        id=str(uuid.uuid4()),
        cognito_sub=str(uuid.uuid4()),
        name="Priya Sharma",
        email="priya@milkful.test",
        role=AdminRole.OPS,
        status=AdminStatus.PENDING,
        ip_allowlist=[],
        max_concurrent_sessions=None,
        created_by=None,
    )
    defaults.update(overrides)
    return AdminUser(**defaults)


def test_create_and_get_by_email(repository):
    created = repository.create(_admin())

    found = repository.get_by_email("PRIYA@milkful.test")  # case-insensitive per spec §7
    assert found is not None
    assert found.id == created.id
    assert found.role == AdminRole.OPS
    assert found.status == AdminStatus.PENDING


def test_get_by_id_returns_none_when_absent(repository):
    assert repository.get_by_id("does-not-exist") is None


def test_get_by_cognito_sub_finds_the_row(repository):
    created = repository.create(_admin(cognito_sub="sub-xyz"))

    found = repository.get_by_cognito_sub("sub-xyz")

    assert found is not None
    assert found.id == created.id


def test_get_by_cognito_sub_returns_none_when_absent(repository):
    assert repository.get_by_cognito_sub("no-such-sub") is None


def test_create_duplicate_email_raises_conflict(repository):
    repository.create(_admin(email="dup@milkful.test", cognito_sub="sub-1"))

    with pytest.raises(AdminEmailExistsError):
        repository.create(_admin(email="dup@milkful.test", cognito_sub="sub-2"))


def test_list_filters_by_role_and_status(repository):
    repository.create(_admin(email="a@milkful.test", cognito_sub="s1", role=AdminRole.OPS, status=AdminStatus.ACTIVE))
    repository.create(
        _admin(email="b@milkful.test", cognito_sub="s2", role=AdminRole.FINANCE, status=AdminStatus.ACTIVE)
    )
    repository.create(
        _admin(email="c@milkful.test", cognito_sub="s3", role=AdminRole.OPS, status=AdminStatus.DEACTIVATED)
    )

    page = repository.list(role="Ops", status="Active", search=None, page=1, page_size=20)

    assert page.total == 1
    assert page.items[0].email == "a@milkful.test"


def test_list_search_matches_name_or_email(repository):
    repository.create(_admin(email="ravi@milkful.test", cognito_sub="s1", name="Ravi Kumar"))
    repository.create(_admin(email="anita@milkful.test", cognito_sub="s2", name="Anita Rao"))

    page = repository.list(role=None, status=None, search="ravi", page=1, page_size=20)

    assert page.total == 1
    assert page.items[0].name == "Ravi Kumar"


def test_list_paginates(repository):
    for i in range(5):
        repository.create(_admin(email=f"user{i}@milkful.test", cognito_sub=f"sub-{i}"))

    page1 = repository.list(role=None, status=None, search=None, page=1, page_size=2)
    page2 = repository.list(role=None, status=None, search=None, page=2, page_size=2)

    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert {i.id for i in page1.items}.isdisjoint({i.id for i in page2.items})


def test_update_role_and_config_partial_update(repository):
    created = repository.create(_admin(email="x@milkful.test", cognito_sub="s1", role=AdminRole.OPS))

    updated = repository.update_role_and_config(
        created.id,
        role="Finance",
        ip_allowlist=None,
        max_concurrent_sessions=None,
        max_concurrent_sessions_set=False,
    )

    assert updated.role == AdminRole.FINANCE
    assert updated.ip_allowlist == []  # untouched, still empty


def test_update_role_and_config_sets_ip_allowlist_and_max_sessions(repository):
    created = repository.create(_admin(email="y@milkful.test", cognito_sub="s1"))

    updated = repository.update_role_and_config(
        created.id,
        role=None,
        ip_allowlist=["10.0.0.0/24"],
        max_concurrent_sessions=3,
        max_concurrent_sessions_set=True,
    )

    assert updated.ip_allowlist == ["10.0.0.0/24"]
    assert updated.max_concurrent_sessions == 3
    assert updated.role == AdminRole.OPS  # untouched


def test_update_role_and_config_can_clear_max_concurrent_sessions(repository):
    created = repository.create(_admin(email="z@milkful.test", cognito_sub="s1", max_concurrent_sessions=5))

    updated = repository.update_role_and_config(
        created.id,
        role=None,
        ip_allowlist=None,
        max_concurrent_sessions=None,
        max_concurrent_sessions_set=True,
    )

    assert updated.max_concurrent_sessions is None


def test_set_status_and_set_last_login_now(repository):
    created = repository.create(_admin(email="w@milkful.test", cognito_sub="s1", status=AdminStatus.PENDING))

    repository.set_status(created.id, "Active")
    repository.set_last_login_now(created.id)

    reloaded = repository.get_by_id(created.id)
    assert reloaded.status == AdminStatus.ACTIVE
    assert reloaded.last_login_at is not None


def test_count_active_super_admins(repository):
    repository.create(
        _admin(email="s1@milkful.test", cognito_sub="s1", role=AdminRole.SUPER_ADMIN, status=AdminStatus.ACTIVE)
    )
    repository.create(
        _admin(email="s2@milkful.test", cognito_sub="s2", role=AdminRole.SUPER_ADMIN, status=AdminStatus.PENDING)
    )
    repository.create(
        _admin(email="s3@milkful.test", cognito_sub="s3", role=AdminRole.OPS, status=AdminStatus.ACTIVE)
    )

    assert repository.count_active_super_admins() == 1


def test_role_check_constraint_rejects_invalid_value(repository, admin_sqlite_engine):
    with pytest.raises(IntegrityError):
        with admin_sqlite_engine.begin() as conn:
            conn.execute(
                admin_user_table.insert().values(
                    id=str(uuid.uuid4()),
                    cognito_sub="bad-sub",
                    name="Bad",
                    email="bad@milkful.test",
                    role="NotARole",
                    status="Active",
                )
            )


def test_get_by_email_fails_closed_on_db_error(repository, admin_sqlite_engine, monkeypatch):
    def _raise(*args, **kwargs):
        raise OperationalError("connect", {}, Exception("db down"))

    monkeypatch.setattr(admin_sqlite_engine, "connect", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        repository.get_by_email("x@milkful.test")
