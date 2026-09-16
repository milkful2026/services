"""Domain-level tests for AdminUserService, using simple in-memory fakes
for the adapter ports — same style as test_otp_service.py /
test_login_service.py."""

import uuid

import pytest

from domain.admin_exceptions import (
    AdminEmailExistsError,
    AdminForbiddenError,
    AdminNotFoundError,
    AdminValidationError,
    InvalidCidrError,
    InvalidRoleError,
    SelfDeactivationError,
)
from domain.admin_models import AdminRole, AdminStatus, AdminUser, AdminUserPage
from domain.admin_users.user_service import AdminUserService


class FakeAdminRepo:
    def __init__(self):
        self.by_id: dict[str, AdminUser] = {}
        self.by_email: dict[str, str] = {}

    def get_by_email(self, email: str):
        admin_id = self.by_email.get(email.lower())
        return self.by_id.get(admin_id) if admin_id else None

    def get_by_id(self, admin_id: str):
        return self.by_id.get(admin_id)

    def create(self, admin: AdminUser) -> AdminUser:
        if admin.email.lower() in self.by_email:
            raise AdminEmailExistsError()
        self.by_id[admin.id] = admin
        self.by_email[admin.email.lower()] = admin.id
        return admin

    def list(self, role, status, search, page, page_size):
        items = list(self.by_id.values())
        if role:
            items = [a for a in items if a.role.value == role]
        if status:
            items = [a for a in items if a.status.value == status]
        if search:
            items = [a for a in items if search.lower() in a.name.lower() or search.lower() in a.email.lower()]
        total = len(items)
        start = (page - 1) * page_size
        return AdminUserPage(items=items[start : start + page_size], total=total, page=page, page_size=page_size)

    def update_role_and_config(self, admin_id, role, ip_allowlist, max_concurrent_sessions, max_concurrent_sessions_set):
        admin = self.by_id[admin_id]
        if role is not None:
            admin.role = AdminRole(role)
        if ip_allowlist is not None:
            admin.ip_allowlist = ip_allowlist
        if max_concurrent_sessions_set:
            admin.max_concurrent_sessions = max_concurrent_sessions
        return admin

    def set_status(self, admin_id, status):
        self.by_id[admin_id].status = AdminStatus(status)


class FakeCognito:
    def __init__(self):
        self.created_emails: list[str] = []
        self.deleted_emails: list[str] = []
        self.groups: dict[str, str] = {}
        self.global_signed_out: list[str] = []
        self.fail_set_group_for: set[str] = set()

    def admin_create_user(self, email: str, name: str) -> str:
        if email in self.created_emails:
            raise AdminEmailExistsError()
        self.created_emails.append(email)
        return f"sub-{email}"

    def admin_delete_user(self, email: str) -> None:
        self.deleted_emails.append(email)
        if email in self.created_emails:
            self.created_emails.remove(email)

    def set_group(self, email: str, role: str) -> None:
        if email in self.fail_set_group_for:
            raise RuntimeError("group service unavailable")
        self.groups[email] = role

    def global_sign_out(self, email: str) -> None:
        self.global_signed_out.append(email)


class FakeSessionRegistry:
    def __init__(self):
        self.invalidated: list[str] = []

    def invalidate_all(self, admin_id: str) -> None:
        self.invalidated.append(admin_id)


class FakeEventPublisher:
    def __init__(self):
        self.events: list[tuple] = []

    def publish_admin_event(self, event_type, payload, correlation_id) -> None:
        self.events.append((event_type, payload, correlation_id))


@pytest.fixture
def deps():
    return {
        "admin_repo": FakeAdminRepo(),
        "cognito": FakeCognito(),
        "session_registry": FakeSessionRegistry(),
        "event_publisher": FakeEventPublisher(),
    }


@pytest.fixture
def service(deps):
    return AdminUserService(**deps)


def _seed_admin(deps, **overrides) -> AdminUser:
    defaults = dict(
        id=str(uuid.uuid4()),
        cognito_sub="sub-1",
        name="Existing Admin",
        email="existing@milkful.test",
        role=AdminRole.OPS,
        status=AdminStatus.ACTIVE,
        ip_allowlist=[],
        max_concurrent_sessions=None,
        created_by=None,
    )
    defaults.update(overrides)
    admin = AdminUser(**defaults)
    deps["admin_repo"].by_id[admin.id] = admin
    deps["admin_repo"].by_email[admin.email.lower()] = admin.id
    return admin


# ---- create_admin ----


def test_create_admin_requires_super_admin(service):
    with pytest.raises(AdminForbiddenError):
        service.create_admin("Ops", "caller-1", "New Admin", "new@milkful.test", "Ops", "corr-1")


def test_create_admin_rejects_invalid_role(service):
    with pytest.raises(InvalidRoleError):
        service.create_admin("SuperAdmin", "caller-1", "New Admin", "new@milkful.test", "NotARole", "corr-1")


def test_create_admin_rejects_invalid_email(service):
    with pytest.raises(AdminValidationError):
        service.create_admin("SuperAdmin", "caller-1", "New Admin", "not-an-email", "Ops", "corr-1")


def test_create_admin_success_creates_cognito_group_aurora_and_emits_event(service, deps):
    created = service.create_admin("SuperAdmin", "caller-1", "New Admin", "New@Milkful.test", "Finance", "corr-1")

    assert created.status == AdminStatus.PENDING
    assert created.role == AdminRole.FINANCE
    assert "new@milkful.test" in deps["cognito"].created_emails
    assert deps["cognito"].groups["new@milkful.test"] == "Finance"
    assert (
        "admin.user.created",
        {"adminId": created.id, "email": "new@milkful.test", "role": "Finance", "createdBy": "caller-1"},
        "corr-1",
    ) in deps["event_publisher"].events


def test_create_admin_compensates_cognito_when_aurora_insert_fails(service, deps):
    _seed_admin(deps, email="dup@milkful.test")
    # Force the Aurora side to look like the email already exists there
    # even though Cognito hasn't seen it yet, simulating an Aurora-only
    # failure after Cognito user creation succeeded.
    original_create = deps["admin_repo"].create

    def _fail_create(admin):
        raise RuntimeError("aurora unavailable")

    deps["admin_repo"].create = _fail_create

    with pytest.raises(RuntimeError):
        service.create_admin("SuperAdmin", "caller-1", "New Admin", "compensated@milkful.test", "Ops", "corr-1")

    assert "compensated@milkful.test" in deps["cognito"].deleted_emails
    assert deps["event_publisher"].events == []


def test_create_admin_compensates_when_group_assignment_fails(service, deps):
    deps["cognito"].fail_set_group_for.add("groupfail@milkful.test")

    with pytest.raises(RuntimeError):
        service.create_admin("SuperAdmin", "caller-1", "New Admin", "groupfail@milkful.test", "Ops", "corr-1")

    assert "groupfail@milkful.test" in deps["cognito"].deleted_emails
    assert deps["event_publisher"].events == []


def test_create_admin_duplicate_email_returns_conflict(service):
    service.create_admin("SuperAdmin", "caller-1", "First", "dup@milkful.test", "Ops", "corr-1")

    with pytest.raises(AdminEmailExistsError):
        service.create_admin("SuperAdmin", "caller-1", "Second", "dup@milkful.test", "Ops", "corr-1")


# ---- list_admins ----


def test_list_admins_requires_super_admin(service):
    with pytest.raises(AdminForbiddenError):
        service.list_admins("Ops", role=None, status=None, search=None, page=1, page_size=20)


def test_list_admins_returns_page(service, deps):
    _seed_admin(deps, email="a@milkful.test", role=AdminRole.OPS)
    _seed_admin(deps, email="b@milkful.test", role=AdminRole.FINANCE)

    page = service.list_admins("SuperAdmin", role="Ops", status=None, search=None, page=1, page_size=20)

    assert page.total == 1
    assert page.items[0].email == "a@milkful.test"


def test_list_admins_rejects_bad_page_size(service):
    with pytest.raises(AdminValidationError):
        service.list_admins("SuperAdmin", role=None, status=None, search=None, page=1, page_size=0)


# ---- update_admin ----


def test_update_admin_requires_super_admin(service, deps):
    target = _seed_admin(deps)
    with pytest.raises(AdminForbiddenError):
        service.update_admin(
            "Ops", target.id, role="Finance", ip_allowlist=None, ip_allowlist_set=False,
            max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
        )


def test_update_admin_not_found(service):
    with pytest.raises(AdminNotFoundError):
        service.update_admin(
            "SuperAdmin", "no-such-id", role=None, ip_allowlist=None, ip_allowlist_set=False,
            max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
        )


def test_update_admin_role_change_emits_event_and_invalidates_session(service, deps):
    target = _seed_admin(deps, role=AdminRole.OPS)

    updated = service.update_admin(
        "SuperAdmin", target.id, role="Finance", ip_allowlist=None, ip_allowlist_set=False,
        max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
    )

    assert updated.role == AdminRole.FINANCE
    assert deps["cognito"].groups[target.email] == "Finance"
    assert target.email in deps["cognito"].global_signed_out
    assert target.id in deps["session_registry"].invalidated
    assert any(evt[0] == "admin.role.assigned" for evt in deps["event_publisher"].events)


def test_update_admin_same_role_does_not_emit_role_assigned_event(service, deps):
    target = _seed_admin(deps, role=AdminRole.OPS)

    service.update_admin(
        "SuperAdmin", target.id, role="Ops", ip_allowlist=None, ip_allowlist_set=False,
        max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
    )

    assert deps["event_publisher"].events == []
    assert deps["cognito"].global_signed_out == []


def test_update_admin_sets_ip_allowlist(service, deps):
    target = _seed_admin(deps)

    updated = service.update_admin(
        "SuperAdmin", target.id, role=None, ip_allowlist=["10.0.0.0/24"], ip_allowlist_set=True,
        max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
    )

    assert updated.ip_allowlist == ["10.0.0.0/24"]


def test_update_admin_rejects_malformed_cidr(service, deps):
    target = _seed_admin(deps)

    with pytest.raises(InvalidCidrError):
        service.update_admin(
            "SuperAdmin", target.id, role=None, ip_allowlist=["not-a-cidr"], ip_allowlist_set=True,
            max_concurrent_sessions=None, max_concurrent_sessions_set=False, correlation_id="corr-1",
        )


# ---- deactivate_admin / reactivate_admin ----


def test_deactivate_admin_requires_super_admin(service, deps):
    target = _seed_admin(deps)
    with pytest.raises(AdminForbiddenError):
        service.deactivate_admin("Ops", "caller-1", target.id, "corr-1")


def test_deactivate_admin_cannot_deactivate_self(service, deps):
    target = _seed_admin(deps)
    with pytest.raises(SelfDeactivationError):
        service.deactivate_admin("SuperAdmin", target.id, target.id, "corr-1")


def test_deactivate_admin_not_found(service):
    with pytest.raises(AdminNotFoundError):
        service.deactivate_admin("SuperAdmin", "caller-1", "no-such-id", "corr-1")


def test_deactivate_admin_success_revokes_sessions_and_emits_event(service, deps):
    target = _seed_admin(deps, status=AdminStatus.ACTIVE)

    result = service.deactivate_admin("SuperAdmin", "caller-1", target.id, "corr-1")

    assert result.status == AdminStatus.DEACTIVATED
    assert target.email in deps["cognito"].global_signed_out
    assert target.id in deps["session_registry"].invalidated
    assert ("admin.user.deactivated", {"adminId": target.id, "email": target.email}, "corr-1") in deps[
        "event_publisher"
    ].events


def test_reactivate_admin_requires_super_admin(service, deps):
    target = _seed_admin(deps, status=AdminStatus.DEACTIVATED)
    with pytest.raises(AdminForbiddenError):
        service.reactivate_admin("Ops", target.id, "corr-1")


def test_reactivate_admin_success_emits_event(service, deps):
    target = _seed_admin(deps, status=AdminStatus.DEACTIVATED)

    result = service.reactivate_admin("SuperAdmin", target.id, "corr-1")

    assert result.status == AdminStatus.ACTIVE
    assert ("admin.user.reactivated", {"adminId": target.id, "email": target.email}, "corr-1") in deps[
        "event_publisher"
    ].events
