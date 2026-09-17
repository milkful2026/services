"""Domain models for the Admin Identity/RBAC feature (MA-129).

Kept in a separate module from domain/models.py (the pre-existing
consumer OTP/social-auth models) so this additive feature never touches
that file's contents — see the hard constraint in the implementation
task not to modify the existing consumer flows.

Plain dataclasses only — no pydantic, no AWS SDK types (services/README.md
§3.4). DTOs (pydantic) live in the handlers/admin_* packages and are
mapped to/from these at the handler boundary.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AdminRole(str, Enum):
    OPS = "Ops"
    FINANCE = "Finance"
    SUPPORT = "Support"
    MARKETING = "Marketing"
    SUPER_ADMIN = "SuperAdmin"


class AdminStatus(str, Enum):
    PENDING = "Pending"
    ACTIVE = "Active"
    DEACTIVATED = "Deactivated"


@dataclass
class AdminUser:
    id: str
    cognito_sub: str
    name: str
    email: str
    role: AdminRole
    status: AdminStatus
    ip_allowlist: list[str] = field(default_factory=list)
    max_concurrent_sessions: int | None = None
    last_login_at: datetime | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AdminUserPage:
    items: list[AdminUser]
    total: int
    page: int
    page_size: int


@dataclass
class LoginChallenge:
    """Ephemeral, single-use-on-success record bridging FR-1's password
    step to FR-2's 2FA step. `cognito_session` is the opaque Session
    string Cognito returns from AdminInitiateAuth when it responds with
    a SOFTWARE_TOKEN_MFA challenge — required to call
    AdminRespondToAuthChallenge for the same challenge later.
    """

    challenge_token: str
    admin_id: str
    email: str
    cognito_session: str
    expires_at: int  # epoch seconds


@dataclass
class AdminTokenBundle:
    access_token: str
    refresh_token: str
    id_token: str
    expires_in: int
