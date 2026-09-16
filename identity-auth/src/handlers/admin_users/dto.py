"""Request/response DTOs for the admin-user-management endpoints
(FR-3/FR-4). Response envelopes reuse the shared helpers in
handlers/dto.py."""

from pydantic import BaseModel, Field

from domain.admin_models import AdminUser, AdminUserPage


class AdminCreateUserRequest(BaseModel):
    name: str
    email: str
    role: str


class AdminUpdateUserRequest(BaseModel):
    role: str | None = None
    ip_allowlist: list[str] | None = Field(default=None, alias="ipAllowlist")
    max_concurrent_sessions: int | None = Field(default=None, alias="maxConcurrentSessions")

    model_config = {"populate_by_name": True}


def admin_to_dict(admin: AdminUser) -> dict:
    return {
        "id": admin.id,
        "name": admin.name,
        "email": admin.email,
        "role": admin.role.value,
        "status": admin.status.value,
        "ipAllowlist": admin.ip_allowlist,
        "maxConcurrentSessions": admin.max_concurrent_sessions,
        "lastLoginAt": admin.last_login_at.isoformat() if admin.last_login_at else None,
        "createdBy": admin.created_by,
        "createdAt": admin.created_at.isoformat() if admin.created_at else None,
        "updatedAt": admin.updated_at.isoformat() if admin.updated_at else None,
    }


def admin_page_to_dict(page: AdminUserPage) -> dict:
    return {
        "items": [admin_to_dict(a) for a in page.items],
        "total": page.total,
        "page": page.page,
        "pageSize": page.page_size,
    }
