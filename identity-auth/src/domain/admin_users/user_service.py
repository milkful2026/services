"""Admin user management business rules (spec FR-3/FR-4): CRUD, the
account-creation saga, and role-change/deactivation side effects.

No AWS SDK imports here (services/README.md §3.4) — only the abstract
adapter Protocols from adapters.admin_interfaces. Authorization
(SuperAdmin-only) is enforced here too, not only at the API Gateway
authorizer layer — services/README.md §5b: "never trust a client-
supplied role", and defense in depth against an authorizer
misconfiguration.
"""

import ipaddress
import logging
import re
import uuid

from adapters.admin_interfaces import (
    AdminCognitoPort,
    AdminEventPublisherPort,
    AdminSessionRegistryPort,
    AdminUserRepositoryPort,
)
from domain.admin_exceptions import (
    AdminForbiddenError,
    AdminNotFoundError,
    AdminValidationError,
    InvalidCidrError,
    InvalidRoleError,
    SelfDeactivationError,
)
from domain.admin_models import AdminRole, AdminStatus, AdminUser, AdminUserPage

logger = logging.getLogger(__name__)

_VALID_ROLES = {r.value for r in AdminRole}
_VALID_STATUSES = {s.value for s in AdminStatus}
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_role(role: str) -> str:
    if role not in _VALID_ROLES:
        raise InvalidRoleError(f"role must be one of {sorted(_VALID_ROLES)}")
    return role


def validate_status_filter(status: str) -> str:
    if status not in _VALID_STATUSES:
        raise AdminValidationError(f"status must be one of {sorted(_VALID_STATUSES)}")
    return status


def validate_cidr_list(cidrs: list[str]) -> list[str]:
    validated: list[str] = []
    for c in cidrs:
        try:
            ipaddress.ip_network(c, strict=False)
        except ValueError as exc:
            raise InvalidCidrError(f"Invalid CIDR range: {c!r}") from exc
        validated.append(c)
    return validated


def _require_super_admin(caller_role: str) -> None:
    if caller_role != AdminRole.SUPER_ADMIN.value:
        raise AdminForbiddenError()


class AdminUserService:
    def __init__(
        self,
        admin_repo: AdminUserRepositoryPort,
        cognito: AdminCognitoPort,
        session_registry: AdminSessionRegistryPort,
        event_publisher: AdminEventPublisherPort,
    ) -> None:
        self._admin_repo = admin_repo
        self._cognito = cognito
        self._session_registry = session_registry
        self._event_publisher = event_publisher

    def create_admin(
        self,
        caller_role: str,
        caller_admin_id: str,
        name: str,
        email: str,
        role: str,
        correlation_id: str,
    ) -> AdminUser:
        """FR-3 — Super-Admin-only invite. Saga: Cognito user created
        first, then the Aurora insert. If the Aurora insert (or the
        Cognito group assignment right after user creation) fails, the
        just-created Cognito user is deleted so no orphaned Cognito
        identity is left with no matching Aurora record. The
        `admin.user.created` event is only emitted after BOTH writes
        have succeeded."""
        _require_super_admin(caller_role)

        name = name.strip()
        email = email.strip().lower()
        if not (2 <= len(name) <= 100):
            raise AdminValidationError("name must be 2-100 characters")
        if not _EMAIL_PATTERN.match(email):
            raise AdminValidationError("Invalid email format")
        validate_role(role)

        # AdminCreateUser's own UsernameExistsException (mapped by the
        # adapter to AdminEmailExistsError) is the authoritative 409
        # check named by spec FR-3 ("already exists in the Admin pool") —
        # no separate Aurora pre-check needed; the double-click race
        # (spec §9) is resolved by this same uniqueness check, not an
        # idempotency key.
        cognito_sub = self._cognito.admin_create_user(email, name)

        try:
            self._cognito.set_group(email, role)
        except Exception:
            logger.error(
                "admin_user_service.create_admin: group assignment failed, compensating",
                extra={"correlationId": correlation_id, "email": email},
            )
            self._cognito.admin_delete_user(email)
            raise

        try:
            created = self._admin_repo.create(
                AdminUser(
                    id=str(uuid.uuid4()),
                    cognito_sub=cognito_sub,
                    name=name,
                    email=email,
                    role=AdminRole(role),
                    status=AdminStatus.PENDING,
                    ip_allowlist=[],
                    max_concurrent_sessions=None,
                    created_by=caller_admin_id,
                )
            )
        except Exception:
            logger.error(
                "admin_user_service.create_admin: Aurora insert failed after Cognito create, "
                "compensating by deleting the Cognito user",
                extra={"correlationId": correlation_id, "email": email},
            )
            self._cognito.admin_delete_user(email)
            raise

        # Invitation email trigger is this event itself (spec §3: "no new
        # notification channel invented") — a future Notification
        # consumer of admin.user.created sends the TOTP-enrollment +
        # password-set email, mirroring OtpRequested's own precedent.
        self._event_publisher.publish_admin_event(
            "admin.user.created",
            {"adminId": created.id, "email": created.email, "role": created.role.value, "createdBy": caller_admin_id},
            correlation_id,
        )
        return created

    def list_admins(
        self,
        caller_role: str,
        role: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> AdminUserPage:
        _require_super_admin(caller_role)
        if role is not None:
            validate_role(role)
        if status is not None:
            validate_status_filter(status)
        if page < 1:
            raise AdminValidationError("page must be >= 1")
        if not (1 <= page_size <= 100):
            raise AdminValidationError("pageSize must be between 1 and 100")
        return self._admin_repo.list(role=role, status=status, search=search, page=page, page_size=page_size)

    def update_admin(
        self,
        caller_role: str,
        target_id: str,
        role: str | None,
        ip_allowlist: list[str] | None,
        ip_allowlist_set: bool,
        max_concurrent_sessions: int | None,
        max_concurrent_sessions_set: bool,
        correlation_id: str,
    ) -> AdminUser:
        """FR-4 PATCH — a role change emits `admin.role.assigned` and
        invalidates the target's active session (AdminUserGlobalSignOut):
        this is the correct primitive for spec §9's documented trade-off
        ("existing access token remains valid until its own expiry, but
        the refresh token is invalidated") — GlobalSignOut only breaks
        future REFRESH_TOKEN_AUTH calls, it does not (and cannot) revoke
        an already-issued, still-unexpired access token's JWT signature."""
        _require_super_admin(caller_role)
        target = self._admin_repo.get_by_id(target_id)
        if target is None:
            raise AdminNotFoundError()

        validated_role = validate_role(role) if role is not None else None
        validated_ips = validate_cidr_list(ip_allowlist or []) if ip_allowlist_set else None
        # Captured BEFORE update_role_and_config runs — a repository
        # implementation that mutates its returned object in place (as
        # some ORMs do) would otherwise make this comparison always see
        # the already-updated value.
        original_role = target.role.value
        target_email = target.email

        updated = self._admin_repo.update_role_and_config(
            target_id,
            role=validated_role,
            ip_allowlist=validated_ips,
            max_concurrent_sessions=max_concurrent_sessions,
            max_concurrent_sessions_set=max_concurrent_sessions_set,
        )

        if validated_role is not None and validated_role != original_role:
            self._cognito.set_group(target_email, validated_role)
            self._cognito.global_sign_out(target_email)
            self._session_registry.invalidate_all(target_id)
            self._event_publisher.publish_admin_event(
                "admin.role.assigned",
                {"adminId": target_id, "email": target_email, "role": validated_role, "changedBy": caller_role},
                correlation_id,
            )

        return updated

    def deactivate_admin(
        self, caller_role: str, caller_admin_id: str, target_id: str, correlation_id: str
    ) -> AdminUser:
        _require_super_admin(caller_role)
        if target_id == caller_admin_id:
            raise SelfDeactivationError()

        target = self._admin_repo.get_by_id(target_id)
        if target is None:
            raise AdminNotFoundError()

        self._cognito.global_sign_out(target.email)
        self._admin_repo.set_status(target_id, AdminStatus.DEACTIVATED.value)
        self._session_registry.invalidate_all(target_id)

        self._event_publisher.publish_admin_event(
            "admin.user.deactivated", {"adminId": target_id, "email": target.email}, correlation_id
        )
        return self._admin_repo.get_by_id(target_id)

    def reactivate_admin(self, caller_role: str, target_id: str, correlation_id: str) -> AdminUser:
        """Does not restore any previously-revoked session (spec FR-4) —
        no session_registry/Cognito call here beyond the status flip."""
        _require_super_admin(caller_role)
        target = self._admin_repo.get_by_id(target_id)
        if target is None:
            raise AdminNotFoundError()

        self._admin_repo.set_status(target_id, AdminStatus.ACTIVE.value)

        self._event_publisher.publish_admin_event(
            "admin.user.reactivated", {"adminId": target_id, "email": target.email}, correlation_id
        )
        return self._admin_repo.get_by_id(target_id)
