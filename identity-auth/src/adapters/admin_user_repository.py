"""SQLAlchemy Core repository for `admin_user` (MA-129 §7), against this
service's own, NEW Aurora database — never `user` service's database
(database-per-service, services/README.md §1).

Same portable-types approach as services/user's user_repository.py: the
same table definition runs against Postgres (production) and an
in-memory SQLite engine (tests), a documented fidelity gap. `ip_allowlist`
is `TEXT[]` in the production-authoritative migration
(migrations/0001_admin_user.sql) but a portable JSON column here, exactly
like that module's own `lines` column.

Per services/README.md §3.7: the only place allowed to import SQLAlchemy
for this concern.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from domain.admin_exceptions import AdminEmailExistsError
from domain.admin_models import AdminRole, AdminStatus, AdminUser, AdminUserPage
from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

metadata = MetaData()

admin_user_table = Table(
    "admin_user",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("cognito_sub", String(128), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("email", String(255), nullable=False, unique=True),
    Column("role", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("ip_allowlist", JSON, nullable=True),
    Column("max_concurrent_sessions", Integer, nullable=True),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(36), ForeignKey("admin_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "role IN ('Ops', 'Finance', 'Support', 'Marketing', 'SuperAdmin')", name="ck_admin_user_role"
    ),
    CheckConstraint("status IN ('Pending', 'Active', 'Deactivated')", name="ck_admin_user_status"),
)


def create_schema(engine: Engine) -> None:
    """Test-only convenience — production schema ownership is the raw SQL
    migration file, not this."""
    metadata.create_all(engine)


def _row_to_admin(row) -> AdminUser:
    return AdminUser(
        id=row.id,
        cognito_sub=row.cognito_sub,
        name=row.name,
        email=row.email,
        role=AdminRole(row.role),
        status=AdminStatus(row.status),
        ip_allowlist=list(row.ip_allowlist) if row.ip_allowlist else [],
        max_concurrent_sessions=row.max_concurrent_sessions,
        last_login_at=row.last_login_at,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyAdminUserRepository:
    def __init__(self, engine: Engine, correlation_id: str = "") -> None:
        self._engine = engine
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def get_by_email(self, email: str) -> AdminUser | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(admin_user_table).where(func.lower(admin_user_table.c.email) == email.lower())
                ).fetchone()
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.get_by_email failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to look up admin user") from exc
        return _row_to_admin(row) if row is not None else None

    def get_by_id(self, admin_id: str) -> AdminUser | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(admin_user_table).where(admin_user_table.c.id == admin_id)
                ).fetchone()
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.get_by_id failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to look up admin user") from exc
        return _row_to_admin(row) if row is not None else None

    def get_by_cognito_sub(self, cognito_sub: str) -> AdminUser | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(admin_user_table).where(admin_user_table.c.cognito_sub == cognito_sub)
                ).fetchone()
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.get_by_cognito_sub failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to look up admin user") from exc
        return _row_to_admin(row) if row is not None else None

    def create(self, admin: AdminUser) -> AdminUser:
        admin_id = admin.id or str(uuid.uuid4())
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    admin_user_table.insert().values(
                        id=admin_id,
                        cognito_sub=admin.cognito_sub,
                        name=admin.name,
                        email=admin.email.lower(),
                        role=admin.role.value if isinstance(admin.role, AdminRole) else admin.role,
                        status=admin.status.value if isinstance(admin.status, AdminStatus) else admin.status,
                        ip_allowlist=admin.ip_allowlist or None,
                        max_concurrent_sessions=admin.max_concurrent_sessions,
                        created_by=admin.created_by,
                    )
                )
        except IntegrityError as exc:
            # Unique constraint on email (or cognito_sub) — the caller
            # (AdminUserService) is expected to have already checked
            # get_by_email as a pre-check for the 409 path; this is the
            # fail-closed guard against the double-click race spec §9
            # calls out explicitly.
            raise AdminEmailExistsError() from exc
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.create failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to persist admin user") from exc

        created = self.get_by_id(admin_id)
        assert created is not None
        return created

    def list(
        self,
        role: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> AdminUserPage:
        conditions = []
        if role:
            conditions.append(admin_user_table.c.role == role)
        if status:
            conditions.append(admin_user_table.c.status == status)
        if search:
            like = f"%{search.lower()}%"
            conditions.append(
                or_(
                    func.lower(admin_user_table.c.name).like(like),
                    func.lower(admin_user_table.c.email).like(like),
                )
            )
        where_clause = and_(*conditions) if conditions else None

        try:
            with self._engine.connect() as conn:
                count_stmt = select(func.count()).select_from(admin_user_table)
                if where_clause is not None:
                    count_stmt = count_stmt.where(where_clause)
                total = conn.execute(count_stmt).scalar_one()

                stmt = select(admin_user_table)
                if where_clause is not None:
                    stmt = stmt.where(where_clause)
                stmt = (
                    stmt.order_by(admin_user_table.c.created_at.desc())
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
                rows = conn.execute(stmt).fetchall()
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.list failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to list admin users") from exc

        return AdminUserPage(
            items=[_row_to_admin(r) for r in rows], total=int(total), page=page, page_size=page_size
        )

    def update_role_and_config(
        self,
        admin_id: str,
        role: str | None,
        ip_allowlist: list[str] | None,
        max_concurrent_sessions: int | None,
        max_concurrent_sessions_set: bool,
    ) -> AdminUser:
        values: dict = {"updated_at": datetime.now(UTC)}
        if role is not None:
            values["role"] = role
        if ip_allowlist is not None:
            values["ip_allowlist"] = ip_allowlist or None
        if max_concurrent_sessions_set:
            values["max_concurrent_sessions"] = max_concurrent_sessions

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(admin_user_table).where(admin_user_table.c.id == admin_id).values(**values)
                )
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.update_role_and_config failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update admin user") from exc

        updated = self.get_by_id(admin_id)
        assert updated is not None
        return updated

    def set_status(self, admin_id: str, status: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(admin_user_table)
                    .where(admin_user_table.c.id == admin_id)
                    .values(status=status, updated_at=datetime.now(UTC))
                )
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.set_status failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update admin user status") from exc

    def set_last_login_now(self, admin_id: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(admin_user_table)
                    .where(admin_user_table.c.id == admin_id)
                    .values(last_login_at=datetime.now(UTC), updated_at=datetime.now(UTC))
                )
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.set_last_login_now failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update last login") from exc

    def count_active_super_admins(self) -> int:
        try:
            with self._engine.connect() as conn:
                total = conn.execute(
                    select(func.count())
                    .select_from(admin_user_table)
                    .where(
                        admin_user_table.c.role == AdminRole.SUPER_ADMIN.value,
                        admin_user_table.c.status == AdminStatus.ACTIVE.value,
                    )
                ).scalar_one()
        except SQLAlchemyError as exc:
            logger.error(
                "admin_user_repository.count_active_super_admins failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to count active Super Admins") from exc
        return int(total)
