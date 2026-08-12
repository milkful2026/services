"""Domain models. Plain dataclasses only — no pydantic, no AWS SDK types.

Per services/README.md §3.4: domain must not import transport frameworks
or AWS SDKs. DTOs (pydantic) live in handlers/dto.py and are mapped to/from
these at the handler boundary.
"""

from dataclasses import dataclass, field
from enum import Enum


class OtpStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    LOCKED = "LOCKED"
    SEND_FAILED = "SEND_FAILED"


@dataclass
class OtpRecord:
    request_id: str
    mobile: str
    otp_hash: str
    attempts: int
    status: OtpStatus
    ttl: int  # epoch seconds
    last_sent_at: int  # epoch seconds
    purpose: str = "REGISTER"  # reserved for MA-21 login; not branched on yet


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    id_token: str
    expires_in: int


@dataclass
class SocialAuthResult:
    """Either `tokens` is populated (linked/created + fully verified), or
    `requires_mobile_verification` is True and `partial_token` is set —
    per spec FR-3 / flagged G1. Never both."""

    tokens: TokenBundle | None = None
    is_new_user: bool = False
    requires_mobile_verification: bool = False
    partial_token: str | None = None
    details: dict = field(default_factory=dict)
