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


def validate_max_concurrent_sessions(value: int) -> int:
    """`0` is a valid, deliberate value ("no sessions allowed") — only
    negative values are rejected."""
    if value < 0:
        raise AdminValidationError("maxConcurrentSessions must be >= 0")
    return value


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
        except Exception as original_exc:
            logger.error(
                "admin_user_service.create_admin: group assignment failed, compensating",
                extra={"correlationId": correlation_id, "email": email},
            )
            self._compensate_create_failure(email, original_exc, correlation_id)

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
        except Exception as original_exc:
            logger.error(
                "admin_user_service.create_admin: Aurora insert failed after Cognito create, "
                "compensating by deleting the Cognito user",
                extra={"correlationId": correlation_id, "email": email},
            )
            self._compensate_create_failure(email, original_exc, correlation_id)

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

    def _compensate_create_failure(self, email: str, original_exc: Exception, correlation_id: str) -> None:
        """Deletes the just-created Cognito user after a downstream
        create_admin step fails. If the compensating delete ITSELF
        raises (e.g. a transient Cognito throttle), that new exception
        must not silently replace `original_exc` in what the caller
        sees — a masked root cause is exactly how an orphaned Cognito
        user (group-assigned or not, with no matching Aurora row) goes
        unnoticed. Logged critically as its own distinct failure mode
        so ops has a durable signal to reconcile, then re-raises the
        original failure either way."""
        try:
            self._cognito.admin_delete_user(email)
        except Exception as compensation_exc:
            logger.critical(
                "admin_user_service.create_admin: compensation FAILED — Cognito user %s is "
                "orphaned (no matching Aurora row) and needs manual cleanup",
                email,
                extra={"correlationId": correlation_id, "email": email},
                exc_info=compensation_exc,
            )
        raise original_exc from original_exc

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
        validated_max_sessions = (
            validate_max_concurrent_sessions(max_concurrent_sessions)
            if max_concurrent_sessions_set and max_concurrent_sessions is not None
            else max_concurrent_sessions
        )
        # Captured BEFORE any write — a repository implementation that
        # mutates its returned object in place (as some ORMs do) would
        # otherwise make this comparison always see the already-updated
        # value.
        original_role = target.role.value
        target_email = target.email
        role_changing = validated_role is not None and validated_role != original_role

        # Cognito Group membership is changed BEFORE the Aurora write
        # (not after, as this previously did): if set_group fails here,
        # nothing has changed in Aurora yet and the request just fails
        # cleanly. The reverse order (Aurora first) risks committing the
        # new role in Aurora and then failing to mirror it into Cognito
        # with no rollback — the two sources of truth this module's own
        # docstring says are "kept in sync on every role change" would
        # silently desync instead.
        if role_changing:
            self._cognito.set_group(target_email, validated_role)

        try:
            updated = self._admin_repo.update_role_and_config(
                target_id,
                role=validated_role,
                ip_allowlist=validated_ips,
                max_concurrent_sessions=validated_max_sessions,
                max_concurrent_sessions_set=max_concurrent_sessions_set,
            )
        except Exception:
            if role_changing:
                # Aurora is the record system list/detail reads come
                # from, so a failure here after Cognito already changed
                # must not leave the two silently desynced. Best-effort
                # revert; if the revert itself fails, that failure is
                # logged critically (not swallowed) so ops can
                # reconcile manually, and the ORIGINAL Aurora error is
                # still what propagates to the caller — a revert
                # failure must not mask the real cause of the request
                # failing.
                try:
                    self._cognito.set_group(target_email, original_role)
                except Exception:
                    logger.critical(
                        "admin_user_service.update_admin: Aurora write failed AND Cognito "
                        "group revert failed — admin is desynced (Cognito=%s, Aurora=%s) "
                        "and needs manual reconciliation",
                        validated_role,
                        original_role,
                        extra={"correlationId": correlation_id, "adminId": target_id, "email": target_email},
                    )
            raise

        if role_changing:
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

        # Aurora status is the authoritative "is this account allowed to
        # log in" record (checked by login_password and, since the
        # verify_2fa status re-check fix, by verify_2fa too) — it's
        # written FIRST, not after global_sign_out as this previously
        # did. That prior ordering meant a transient Aurora failure
        # after global_sign_out already succeeded left the *current*
        # session killed while the account still showed Active, so a
        # caller told "deactivation failed" would wrongly believe the
        # account still had access. With status written first, a
        # failure here is a clean no-op: Cognito hasn't been touched.
        self._admin_repo.set_status(target_id, AdminStatus.DEACTIVATED.value)

        # Killing the *current* session is best-effort cleanup once the
        # authoritative status change has already landed — new logins
        # and refreshes are already blocked by the status check above,
        # so a failure here (logged, not raised) must not make this
        # request report "deactivation failed" when the account is in
        # fact already deactivated.
        try:
            self._cognito.global_sign_out(target.email)
        except Exception:
            logger.warning(
                "admin_user_service.deactivate_admin: account deactivated but session "
                "revocation failed — existing refresh token remains valid until natural "
                "expiry; manual Cognito reconciliation may be needed",
                extra={"correlationId": correlation_id, "adminId": target_id, "email": target.email},
            )
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
