"""Customer account status domain service (MA-139) - status-transition
rules (spec section 9's table), bulk orchestration, history-row
construction, and the FR-7 suspension sweep.

Kept in a separate module from registration_service.py (mirrors
identity-auth's domain/admin_users/user_service.py living alongside its
own pre-existing domain/otp_service.py) so the existing registration flow
is never touched by this additive feature.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from adapters.interfaces import CognitoAttributePort, UserRepositoryPort
from domain.exceptions import (
    CognitoSyncFailedError,
    CustomerNotFoundError,
    ExternalServiceUnavailableError,
    InvalidStatusTransitionError,
    UserServiceError,
    ValidationError,
)
from domain.models import BulkStatusResult, CustomerAccount, CustomerPage, CustomerStatus

logger = logging.getLogger(__name__)

_VALID_STATUSES = {s.value for s in CustomerStatus}
_BULK_ACTIONS = {"suspend", "deactivate", "reactivate"}
_SWEEP_ACTOR = "system:suspension-sweep"


def _validate_status_filter(status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValidationError(f"status must be one of {sorted(_VALID_STATUSES)}")


def _validate_reason(reason: str | None) -> None:
    if not reason or not reason.strip():
        raise ValidationError("reason is required")


def _validate_until(until: date | None, today: date) -> None:
    if until is None:
        raise ValidationError("until is required for suspend")
    if until <= today:
        raise ValidationError("until must be a future date")


class CustomerStatusService:
    def __init__(
        self,
        user_repository: UserRepositoryPort,
        cognito_attributes: CognitoAttributePort,
        correlation_id: str = "",
    ) -> None:
        self._user_repository = user_repository
        self._cognito_attributes = cognito_attributes
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id
        self._user_repository.set_correlation_id(correlation_id)
        self._cognito_attributes.set_correlation_id(correlation_id)

    # --- FR-1/FR-2: read APIs ---

    def list_customers(
        self, status: str | None, search: str | None, page: int, page_size: int
    ) -> CustomerPage:
        if status is not None:
            _validate_status_filter(status)
        if page < 1:
            raise ValidationError("page must be >= 1")
        if not (1 <= page_size <= 100):
            raise ValidationError("pageSize must be between 1 and 100")
        return self._user_repository.list_customers(status, search, page, page_size)

    def get_customer_detail(self, customer_id: str) -> CustomerAccount:
        account = self._get_or_404(customer_id)
        account.status_history = self._user_repository.get_status_history(customer_id)
        return account

    # --- FR-3: suspend ---

    def suspend(
        self,
        customer_id: str,
        reason: str,
        until: date,
        actor_admin_id: str,
        now: datetime | None = None,
    ) -> CustomerAccount:
        now = now or datetime.now(UTC)
        today = now.date()
        account = self._get_or_404(customer_id)

        if account.status == CustomerStatus.DEACTIVATED.value:
            raise InvalidStatusTransitionError(
                "Cannot suspend a deactivated account -- reactivate it first"
            )

        if account.status == CustomerStatus.SUSPENDED.value and account.suspended_until == until:
            # Idempotent -- spec section 9's "already in that exact
            # status" row: for Suspend this explicitly means the same
            # `until` too (FR-3), not just the same coarse status. No
            # duplicate history row, no duplicate event -- but this check
            # runs BEFORE validation, and AdminDisableUser is still
            # called on this path (FR-3/FR-4, section 11 Risk 1's
            # "load-bearing detail"): a repeat call with a blank/invalid
            # reason, or a by-now-past `until` that still matches the
            # currently-stored value, must still return 200 unchanged
            # rather than raising ValidationError, and must still
            # re-attempt the Cognito call so a retry after a prior
            # Cognito failure actually re-syncs Cognito instead of the
            # idempotency check silently swallowing every retry.
            self._sync_cognito_disable(customer_id, account.cognito_sub)
            return account

        # A real transition (fresh suspend, or a re-suspend with a
        # different `until` -- FR-3's "extending or shortening the
        # suspension" case) only ever reaches here, so validation only
        # runs on the path that will actually perform one.
        _validate_reason(reason)
        _validate_until(until, today)

        updated = self._write_status_change(
            account,
            new_status=CustomerStatus.SUSPENDED.value,
            reason=reason,
            status_effective_from=today,
            history_effective_from=today,
            suspended_until=until,
            actor_admin_id=actor_admin_id,
        )
        self._sync_cognito_disable(customer_id, updated.cognito_sub)
        return updated

    # --- FR-4: deactivate ---

    def deactivate(
        self,
        customer_id: str,
        reason: str,
        actor_admin_id: str,
        now: datetime | None = None,
    ) -> CustomerAccount:
        now = now or datetime.now(UTC)
        today = now.date()
        account = self._get_or_404(customer_id)

        if account.status == CustomerStatus.DEACTIVATED.value:
            # Idempotent -- spec section 4 FR-4: "returns 200 with the
            # existing state unchanged (not an error)", which FR-6's
            # bulk action relies on. This check runs BEFORE validation,
            # so a repeat call with a blank/invalid reason still returns
            # 200 unchanged instead of raising ValidationError. The
            # idempotent path still calls AdminDisableUser (section
            # 6/11 Risk 1's load-bearing detail) -- this is what makes a
            # retry after a prior Cognito-call failure actually
            # re-attempt Cognito, rather than this idempotency check
            # silently swallowing every subsequent retry with no Cognito
            # call at all.
            self._sync_cognito_disable(customer_id, account.cognito_sub)
            return account

        _validate_reason(reason)

        # Deactivating an already-Suspended account is allowed --
        # Deactivated supersedes Suspended; suspended_until is cleared
        # (spec section 9).
        updated = self._write_status_change(
            account,
            new_status=CustomerStatus.DEACTIVATED.value,
            reason=reason,
            status_effective_from=today,
            history_effective_from=today,
            suspended_until=None,
            actor_admin_id=actor_admin_id,
        )
        self._sync_cognito_disable(customer_id, updated.cognito_sub)
        return updated

    # --- FR-5: reactivate ---

    def reactivate(
        self,
        customer_id: str,
        reason: str | None,
        actor_admin_id: str,
        now: datetime | None = None,
    ) -> CustomerAccount:
        """Does not touch any of the customer's subscriptions (D2) -- this
        method only ever changes `users.status` and Cognito state (spec
        section 9's own edge case)."""
        now = now or datetime.now(UTC)
        today = now.date()
        account = self._get_or_404(customer_id)

        if account.status == CustomerStatus.ACTIVE.value:
            # Same idempotency posture as suspend/deactivate above --
            # spec section 9 generalizes "already in that exact status"
            # to every transition, not just FR-4's explicitly-worded
            # case, including still re-attempting the Cognito call
            # (AdminEnableUser) on this path for the same retry-safety
            # reason as FR-3/FR-4. `reason` is optional for reactivate
            # to begin with, so there's no validation to reorder here --
            # only the missing Cognito call on this path needed fixing.
            self._sync_cognito_enable(customer_id, account.cognito_sub)
            return account

        updated = self._write_status_change(
            account,
            new_status=CustomerStatus.ACTIVE.value,
            reason=reason,
            status_effective_from=today,
            # Spec section 7: effective_from is null in the HISTORY row
            # for a reactivation, even though the `users` column itself
            # is still stamped with today (see update_customer_status's
            # own docstring for why these two are separate parameters).
            history_effective_from=None,
            suspended_until=None,
            actor_admin_id=actor_admin_id,
        )
        self._sync_cognito_enable(customer_id, updated.cognito_sub)
        return updated

    # --- FR-6: bulk ---

    def bulk_status_change(
        self,
        customer_ids: list[str],
        action: str,
        reason: str | None,
        until: date | None,
        actor_admin_id: str,
        now: datetime | None = None,
    ) -> list[BulkStatusResult]:
        """Applies FR-3/4/5's single-account logic once per id,
        independently -- each call below is its own DB transaction (via
        update_customer_status), so one row's failure (a 404, a 409, a
        Cognito timeout) can never roll back another row. Matches AC-6
        by construction, not by special-casing (spec section 4 FR-6)."""
        if action not in _BULK_ACTIONS:
            raise ValidationError(f"action must be one of {sorted(_BULK_ACTIONS)}")
        now = now or datetime.now(UTC)

        results: list[BulkStatusResult] = []
        for customer_id in customer_ids:
            try:
                if action == "suspend":
                    self.suspend(customer_id, reason, until, actor_admin_id, now=now)
                elif action == "deactivate":
                    self.deactivate(customer_id, reason, actor_admin_id, now=now)
                else:
                    self.reactivate(customer_id, reason, actor_admin_id, now=now)
                results.append(BulkStatusResult(customer_id=customer_id, success=True))
            except UserServiceError as exc:
                logger.info(
                    "customer_status_service.bulk_status_change: row failed",
                    extra={
                        "correlationId": self._correlation_id,
                        "customerId": customer_id,
                        "errorCode": exc.error_code,
                    },
                )
                results.append(
                    BulkStatusResult(customer_id=customer_id, success=False, error_code=exc.error_code)
                )
        return results

    # --- FR-7: suspension sweep ---

    def run_suspension_sweep(self, now: datetime | None = None) -> int:
        """Scheduled entrypoint (see handlers/suspension_sweep_handler.py)
        -- queries every Suspended account whose suspended_until has
        passed and auto-lifts it. One account's failure (Cognito outage,
        a stray DB error) is logged and skipped, not allowed to abort the
        rest of the sweep -- same "one bad row must not stop the batch"
        posture as subscription_service.run_daily and
        outbox_publisher_handler in this same codebase.

        Also runs a second, independent pass (code-review fix, not
        itself part of MA-139's spec): re-attempts the Cognito call for
        every account flagged `cognito_sync_pending` -- see
        _reconcile_cognito_sync_drift's own docstring. Kept as a second
        pass over a distinct candidate set, not merged into the loop
        above, since the two are unrelated (an expired suspension vs. a
        stuck Cognito call) and the drift set can include Deactivated or
        even Active accounts the expired-suspension query would never
        touch."""
        now = now or datetime.now(UTC)
        today = now.date()
        candidates = self._user_repository.list_expired_suspensions(today)

        lifted = 0
        for account in candidates:
            try:
                updated = self._write_status_change(
                    account,
                    new_status=CustomerStatus.ACTIVE.value,
                    reason=None,
                    status_effective_from=today,
                    history_effective_from=None,
                    suspended_until=None,
                    actor_admin_id=_SWEEP_ACTOR,
                )
                self._sync_cognito_enable(updated.id, updated.cognito_sub)
                lifted += 1
            except UserServiceError as exc:
                logger.error(
                    "customer_status_service.run_suspension_sweep: failed for one account, continuing",
                    extra={
                        "correlationId": self._correlation_id,
                        "customerId": account.id,
                        "errorCode": exc.error_code,
                    },
                )
        logger.info(
            "user.suspension_sweep_lifted_count",
            extra={
                "metric": "user.suspension_sweep_lifted_count",
                "correlationId": self._correlation_id,
                "count": lifted,
            },
        )

        self._reconcile_cognito_sync_drift()

        return lifted

    def _reconcile_cognito_sync_drift(self) -> None:
        """Code-review fix for MA-139's FR-3/FR-4/section 11 Risk 1 gap:
        the spec's own "compensating-retry contract" relies entirely on
        an admin (or FR-6 bulk caller) noticing a 502/COGNITO_SYNC_FAILED
        response and manually retrying the same action -- there was no
        automated reconciliation at all. This is a deliberately narrow,
        best-effort mitigation, not full automated reconciliation (that
        would need a way to read Cognito's actual current enabled/
        disabled state and compare it against `status`, which is a
        larger change than this fix pass -- see
        CognitoAttributePort/cognito_attribute_adapter.py, which has no
        such read method today): it just gives every account whose last
        sync attempt is known to have failed (`cognito_sync_pending`,
        set by _mark_cognito_sync_pending) one more retry per day,
        piggybacking on the FR-7 sweep's existing schedule rather than
        adding a new one. A still-failing account stays flagged and is
        picked up again on the next run; this method itself never raises
        -- one account's continued failure must not block the rest, same
        posture as the expired-suspension loop above."""
        pending = self._user_repository.list_cognito_sync_pending()
        for account in pending:
            try:
                if account.status == CustomerStatus.ACTIVE.value:
                    self._sync_cognito_enable(account.id, account.cognito_sub)
                else:
                    self._sync_cognito_disable(account.id, account.cognito_sub)
            except CognitoSyncFailedError:
                # Still drifted -- _sync_cognito_disable/_enable already
                # logged it and re-set the pending flag; picked up again
                # on tomorrow's sweep.
                continue

    # --- shared helpers ---

    def _get_or_404(self, customer_id: str) -> CustomerAccount:
        account = self._user_repository.get_customer_by_id(customer_id)
        if account is None:
            raise CustomerNotFoundError(f"No customer {customer_id!r}")
        return account

    def _write_status_change(
        self,
        account: CustomerAccount,
        *,
        new_status: str,
        reason: str | None,
        status_effective_from: date | None,
        history_effective_from: date | None,
        suspended_until: date | None,
        actor_admin_id: str,
    ) -> CustomerAccount:
        # Same transactional-outbox shape register() already uses
        # (adapters/user_repository.py): the users update, the
        # user_status_history insert, and the outbox_events insert are
        # one DB transaction (spec section 6/9) -- a status change is
        # never partially applied.
        outbox_payload = {
            "userId": account.id,
            "previousStatus": account.status,
            "newStatus": new_status,
            "reason": reason,
            "effectiveFrom": history_effective_from.isoformat() if history_effective_from else None,
            "actorAdminId": actor_admin_id,
        }
        return self._user_repository.update_customer_status(
            account.id,
            new_status=new_status,
            status_reason=reason,
            status_effective_from=status_effective_from,
            history_effective_from=history_effective_from,
            suspended_until=suspended_until,
            actor_admin_id=actor_admin_id,
            outbox_event_type="user.status.changed",
            outbox_payload=outbox_payload,
        )

    def _sync_cognito_disable(self, customer_id: str, cognito_sub: str) -> None:
        # Deliberately AFTER the DB transaction already committed (spec
        # section 6/11 point 1) -- see CognitoSyncFailedError's own
        # docstring for why this ordering, and why a failure here is a
        # distinct 502 rather than the generic
        # ExternalServiceUnavailableError.
        try:
            self._cognito_attributes.disable_user(cognito_sub)
        except ExternalServiceUnavailableError as exc:
            logger.error(
                "customer_status_service: DB committed but Cognito disable_user failed",
                extra={
                    "metric": "user.cognito_sync_drift.count",
                    "correlationId": self._correlation_id,
                    "customerId": customer_id,
                    "intendedCognitoState": "disabled",
                },
            )
            self._mark_cognito_sync_pending(customer_id, True)
            raise CognitoSyncFailedError(
                "Status was updated but disabling the account's login failed -- retry this action"
            ) from exc
        else:
            self._mark_cognito_sync_pending(customer_id, False)

    def _sync_cognito_enable(self, customer_id: str, cognito_sub: str) -> None:
        try:
            self._cognito_attributes.enable_user(cognito_sub)
        except ExternalServiceUnavailableError as exc:
            logger.error(
                "customer_status_service: DB committed but Cognito enable_user failed",
                extra={
                    "metric": "user.cognito_sync_drift.count",
                    "correlationId": self._correlation_id,
                    "customerId": customer_id,
                    "intendedCognitoState": "enabled",
                },
            )
            self._mark_cognito_sync_pending(customer_id, True)
            raise CognitoSyncFailedError(
                "Status was updated but re-enabling the account's login failed -- retry this action"
            ) from exc
        else:
            self._mark_cognito_sync_pending(customer_id, False)

    def _mark_cognito_sync_pending(self, customer_id: str, pending: bool) -> None:
        """Best-effort -- a failure writing this flag must never mask the
        real Cognito result (success or CognitoSyncFailedError) that
        triggered this call; it only means
        _reconcile_cognito_sync_drift's sweep pass won't pick this
        account up (or won't stop retrying it) until its next sync
        attempt successfully updates the flag."""
        try:
            self._user_repository.set_cognito_sync_pending(customer_id, pending)
        except UserServiceError:
            logger.error(
                "customer_status_service: failed to update cognito_sync_pending flag",
                extra={"correlationId": self._correlation_id, "customerId": customer_id},
            )