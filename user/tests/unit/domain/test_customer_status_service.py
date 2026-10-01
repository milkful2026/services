from datetime import date, datetime, timedelta

import pytest

from domain.customer_status_service import CustomerStatusService
from domain.exceptions import (
    CognitoSyncFailedError,
    CustomerNotFoundError,
    ExternalServiceUnavailableError,
    InvalidStatusTransitionError,
    ValidationError,
)
from domain.models import CustomerAccount, CustomerPage, CustomerStatus


class FakeCustomerRepository:
    def __init__(self, accounts: list[CustomerAccount] | None = None):
        self.accounts = {a.id: a for a in (accounts or [])}
        self.history: dict[str, list] = {}
        self.correlation_id = ""
        self.update_calls: list[dict] = []
        self.pending_calls: list[tuple] = []

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def list_customers(self, status, search, page, page_size):
        items = list(self.accounts.values())
        if status is not None:
            items = [a for a in items if a.status == status]
        return CustomerPage(items=items, total=len(items), page=page, page_size=page_size)

    def get_customer_by_id(self, customer_id):
        return self.accounts.get(customer_id)

    def get_status_history(self, customer_id):
        return self.history.get(customer_id, [])

    def update_customer_status(
        self,
        customer_id,
        *,
        new_status,
        status_reason,
        status_effective_from,
        history_effective_from,
        suspended_until,
        actor_admin_id,
        outbox_event_type,
        outbox_payload,
    ):
        self.update_calls.append(
            dict(
                customer_id=customer_id,
                new_status=new_status,
                status_reason=status_reason,
                status_effective_from=status_effective_from,
                history_effective_from=history_effective_from,
                suspended_until=suspended_until,
                actor_admin_id=actor_admin_id,
                outbox_event_type=outbox_event_type,
                outbox_payload=outbox_payload,
            )
        )
        account = self.accounts.get(customer_id)
        if account is None:
            raise CustomerNotFoundError(f"No customer {customer_id!r}")
        account.status = new_status
        account.status_reason = status_reason
        account.suspended_until = suspended_until
        return account

    def list_expired_suspensions(self, as_of: date):
        return [
            a
            for a in self.accounts.values()
            if a.status == CustomerStatus.SUSPENDED.value
            and a.suspended_until is not None
            and a.suspended_until <= as_of
        ]

    def set_cognito_sync_pending(self, customer_id, pending):
        self.pending_calls.append((customer_id, pending))
        account = self.accounts.get(customer_id)
        if account is not None:
            account.cognito_sync_pending = pending

    def list_cognito_sync_pending(self):
        return [a for a in self.accounts.values() if a.cognito_sync_pending]


class FakeCognitoAttributes:
    def __init__(self, disable_raises=None, enable_raises=None):
        self.disable_calls: list[str] = []
        self.enable_calls: list[str] = []
        self.disable_raises = disable_raises
        self.enable_raises = enable_raises
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_mobile_by_sub(self, cognito_sub):  # pragma: no cover — unused by this service
        return None

    def sync_profile_attributes(self, *args, **kwargs):  # pragma: no cover — unused
        return None

    def disable_user(self, cognito_sub: str) -> None:
        self.disable_calls.append(cognito_sub)
        if self.disable_raises:
            raise self.disable_raises

    def enable_user(self, cognito_sub: str) -> None:
        self.enable_calls.append(cognito_sub)
        if self.enable_raises:
            raise self.enable_raises


def _account(**overrides) -> CustomerAccount:
    defaults = dict(
        id="cust-1",
        name="Priya",
        mobile="+919876543210",
        email="priya@example.com",
        account_type="B2C",
        status=CustomerStatus.ACTIVE.value,
        status_reason=None,
        last_status_change_at=None,
        cognito_sub="11111111-2222-3333-4444-555555555555",
        suspended_until=None,
    )
    defaults.update(overrides)
    return CustomerAccount(**defaults)


_NOW = datetime(2026, 9, 30, 12, 0, 0)
_TOMORROW = (_NOW + timedelta(days=1)).date()
_YESTERDAY = (_NOW - timedelta(days=1)).date()


def _service(accounts):
    repo = FakeCustomerRepository(accounts)
    cognito = FakeCognitoAttributes()
    return CustomerStatusService(repo, cognito), repo, cognito


# --- FR-3: suspend ---


def test_suspend_updates_status_and_disables_cognito():
    account = _account()
    service, repo, cognito = _service([account])

    result = service.suspend("cust-1", "fraud suspected", _TOMORROW, "admin-1", now=_NOW)

    assert result.status == CustomerStatus.SUSPENDED.value
    assert cognito.disable_calls == [account.cognito_sub]
    assert len(repo.update_calls) == 1
    call = repo.update_calls[0]
    assert call["new_status"] == CustomerStatus.SUSPENDED.value
    assert call["suspended_until"] == _TOMORROW
    assert call["status_effective_from"] == _NOW.date()
    assert call["history_effective_from"] == _NOW.date()
    assert call["outbox_payload"]["previousStatus"] == CustomerStatus.ACTIVE.value
    assert call["outbox_payload"]["newStatus"] == CustomerStatus.SUSPENDED.value
    assert call["outbox_event_type"] == "user.status.changed"


def test_suspend_requires_a_reason():
    service, _, _ = _service([_account()])
    with pytest.raises(ValidationError):
        service.suspend("cust-1", "", _TOMORROW, "admin-1", now=_NOW)


def test_suspend_requires_a_future_until_date():
    service, _, _ = _service([_account()])
    with pytest.raises(ValidationError):
        service.suspend("cust-1", "reason", _YESTERDAY, "admin-1", now=_NOW)


def test_suspend_rejects_missing_until():
    service, _, _ = _service([_account()])
    with pytest.raises(ValidationError):
        service.suspend("cust-1", "reason", None, "admin-1", now=_NOW)


def test_suspend_already_deactivated_raises_invalid_transition():
    account = _account(status=CustomerStatus.DEACTIVATED.value)
    service, repo, cognito = _service([account])

    with pytest.raises(InvalidStatusTransitionError):
        service.suspend("cust-1", "reason", _TOMORROW, "admin-1", now=_NOW)

    assert repo.update_calls == []
    assert cognito.disable_calls == []


def test_suspend_already_suspended_is_idempotent_noop():
    account = _account(status=CustomerStatus.SUSPENDED.value, suspended_until=_TOMORROW)
    service, repo, cognito = _service([account])

    result = service.suspend("cust-1", "reason", _TOMORROW, "admin-1", now=_NOW)

    assert result.status == CustomerStatus.SUSPENDED.value
    assert repo.update_calls == []  # no duplicate history row/event
    # Still re-attempts Cognito on the idempotent path (spec section 11
    # Risk 1's "load-bearing detail") -- this is what makes a retry
    # after a prior Cognito failure actually re-sync Cognito.
    assert cognito.disable_calls == [account.cognito_sub]


def test_suspend_already_suspended_with_blank_reason_is_still_idempotent_200():
    # Regression test: the idempotency check must run BEFORE validation.
    # A repeat call with a blank reason and an `until` that, relative to
    # `now`, is no longer in the future -- but matches the currently
    # stored suspended_until exactly -- must still return 200 unchanged,
    # not raise ValidationError.
    account = _account(status=CustomerStatus.SUSPENDED.value, suspended_until=_YESTERDAY)
    service, repo, cognito = _service([account])

    result = service.suspend("cust-1", "", _YESTERDAY, "admin-1", now=_NOW)

    assert result.status == CustomerStatus.SUSPENDED.value
    assert repo.update_calls == []
    assert cognito.disable_calls == [account.cognito_sub]


def test_suspend_already_suspended_with_different_until_updates_and_writes_history():
    # FR-3: re-suspending with a DIFFERENT `until` is not a full no-op --
    # suspended_until is updated and a new history row is written.
    account = _account(status=CustomerStatus.SUSPENDED.value, suspended_until=_TOMORROW)
    service, repo, cognito = _service([account])
    new_until = _TOMORROW + timedelta(days=10)

    result = service.suspend("cust-1", "extended", new_until, "admin-1", now=_NOW)

    assert result.status == CustomerStatus.SUSPENDED.value
    assert len(repo.update_calls) == 1
    assert repo.update_calls[0]["suspended_until"] == new_until
    assert cognito.disable_calls == [account.cognito_sub]


def test_suspend_unknown_customer_raises_404():
    service, _, _ = _service([])
    with pytest.raises(CustomerNotFoundError):
        service.suspend("missing", "reason", _TOMORROW, "admin-1", now=_NOW)


def test_suspend_cognito_failure_after_commit_raises_502_but_db_already_updated():
    account = _account()
    repo = FakeCustomerRepository([account])
    cognito = FakeCognitoAttributes(
        disable_raises=ExternalServiceUnavailableError("cognito down")
    )
    service = CustomerStatusService(repo, cognito)

    with pytest.raises(CognitoSyncFailedError):
        service.suspend("cust-1", "reason", _TOMORROW, "admin-1", now=_NOW)

    # DB transaction already committed (spec section 6/11) -- retry-safe.
    assert len(repo.update_calls) == 1
    assert repo.accounts["cust-1"].status == CustomerStatus.SUSPENDED.value
    # Code-review fix: the drift-tracking flag is set so the FR-7 sweep's
    # reconciliation pass will retry this account later.
    assert repo.accounts["cust-1"].cognito_sync_pending is True


# --- FR-4: deactivate ---


def test_deactivate_updates_status_and_disables_cognito():
    account = _account()
    service, repo, cognito = _service([account])

    result = service.deactivate("cust-1", "closure requested", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.DEACTIVATED.value
    assert repo.update_calls[0]["suspended_until"] is None
    assert cognito.disable_calls == [account.cognito_sub]


def test_deactivate_already_deactivated_is_idempotent_200_not_an_error():
    account = _account(status=CustomerStatus.DEACTIVATED.value, status_reason="old reason")
    service, repo, cognito = _service([account])

    result = service.deactivate("cust-1", "new reason", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.DEACTIVATED.value
    assert result.status_reason == "old reason"  # unchanged -- no-op
    assert repo.update_calls == []
    # Still re-attempts Cognito on the idempotent path (FR-4, spec
    # section 11 Risk 1's "load-bearing detail").
    assert cognito.disable_calls == [account.cognito_sub]


def test_deactivate_already_deactivated_with_blank_reason_is_still_idempotent_200():
    # Regression test: the idempotency check must run BEFORE
    # _validate_reason -- a repeat call with a blank reason against an
    # already-deactivated account must still return 200 unchanged, not
    # raise ValidationError.
    account = _account(status=CustomerStatus.DEACTIVATED.value, status_reason="old reason")
    service, repo, cognito = _service([account])

    result = service.deactivate("cust-1", "", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.DEACTIVATED.value
    assert result.status_reason == "old reason"
    assert repo.update_calls == []
    assert cognito.disable_calls == [account.cognito_sub]


def test_deactivate_from_suspended_clears_suspended_until():
    account = _account(status=CustomerStatus.SUSPENDED.value, suspended_until=_TOMORROW)
    service, repo, _ = _service([account])

    result = service.deactivate("cust-1", "reason", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.DEACTIVATED.value
    assert repo.update_calls[0]["suspended_until"] is None


def test_deactivate_requires_a_reason():
    service, _, _ = _service([_account()])
    with pytest.raises(ValidationError):
        service.deactivate("cust-1", "", "admin-1", now=_NOW)


# --- FR-5: reactivate ---


def test_reactivate_enables_cognito_and_clears_suspended_until():
    account = _account(status=CustomerStatus.SUSPENDED.value, suspended_until=_TOMORROW)
    service, repo, cognito = _service([account])

    result = service.reactivate("cust-1", "appeal approved", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.ACTIVE.value
    call = repo.update_calls[0]
    assert call["suspended_until"] is None
    # Spec section 7: history row's effective_from is null for a
    # reactivation, even though the users column is still stamped.
    assert call["history_effective_from"] is None
    assert call["status_effective_from"] == _NOW.date()
    assert cognito.enable_calls == [account.cognito_sub]


def test_reactivate_reason_is_optional():
    account = _account(status=CustomerStatus.DEACTIVATED.value)
    service, repo, _ = _service([account])

    result = service.reactivate("cust-1", None, "admin-1", now=_NOW)
    assert result.status == CustomerStatus.ACTIVE.value


def test_reactivate_already_active_is_noop():
    account = _account(status=CustomerStatus.ACTIVE.value)
    service, repo, cognito = _service([account])

    result = service.reactivate("cust-1", "reason", "admin-1", now=_NOW)

    assert result.status == CustomerStatus.ACTIVE.value
    assert repo.update_calls == []
    # Still re-attempts Cognito on the idempotent path, same posture as
    # suspend/deactivate above.
    assert cognito.enable_calls == [account.cognito_sub]


def test_reactivate_does_not_touch_subscriptions():
    """D2 — reactivating the account never calls anything Subscription-
    related; this domain service has no such collaborator at all, so the
    absence of one IS the guarantee (spec section 9's own edge case)."""
    account = _account(status=CustomerStatus.DEACTIVATED.value)
    service, _, _ = _service([account])
    assert not hasattr(service, "_subscription_client")
    service.reactivate("cust-1", "reason", "admin-1", now=_NOW)


# --- FR-6: bulk ---


def test_bulk_status_change_one_bad_id_does_not_roll_back_others():
    good = _account(id="cust-good", cognito_sub="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    service, repo, cognito = _service([good])

    results = service.bulk_status_change(
        ["cust-good", "cust-missing"], "deactivate", "reason", None, "admin-1", now=_NOW
    )

    by_id = {r.customer_id: r for r in results}
    assert by_id["cust-good"].success is True
    assert by_id["cust-missing"].success is False
    assert by_id["cust-missing"].error_code == "CUSTOMER_NOT_FOUND"
    # The good row's own transaction still committed despite the other
    # row's failure — no shared transaction across rows.
    assert repo.accounts["cust-good"].status == CustomerStatus.DEACTIVATED.value


def test_bulk_status_change_rejects_unknown_action():
    service, _, _ = _service([_account()])
    with pytest.raises(ValidationError):
        service.bulk_status_change(["cust-1"], "not-a-real-action", "reason", None, "admin-1")


def test_bulk_status_change_suspend_applies_each_independently():
    a = _account(id="a", cognito_sub="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = _account(id="b", cognito_sub="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    service, repo, cognito = _service([a, b])

    results = service.bulk_status_change(
        ["a", "b"], "suspend", "reason", _TOMORROW, "admin-1", now=_NOW
    )

    assert all(r.success for r in results)
    assert repo.accounts["a"].status == CustomerStatus.SUSPENDED.value
    assert repo.accounts["b"].status == CustomerStatus.SUSPENDED.value
    assert set(cognito.disable_calls) == {a.cognito_sub, b.cognito_sub}


# --- FR-1/FR-2: read APIs ---


def test_list_customers_validates_status_filter():
    service, _, _ = _service([])
    with pytest.raises(ValidationError):
        service.list_customers("NotAStatus", None, 1, 20)


def test_list_customers_validates_page_size():
    service, _, _ = _service([])
    with pytest.raises(ValidationError):
        service.list_customers(None, None, 1, 1000)


def test_get_customer_detail_not_found_raises_404():
    service, _, _ = _service([])
    with pytest.raises(CustomerNotFoundError):
        service.get_customer_detail("missing")


def test_get_customer_detail_populates_history():
    account = _account()
    service, repo, _ = _service([account])
    repo.history["cust-1"] = ["fake-history-entry"]

    result = service.get_customer_detail("cust-1")
    assert result.status_history == ["fake-history-entry"]


# --- FR-7: suspension sweep ---


def test_suspension_sweep_lifts_expired_suspensions():
    expired = _account(
        id="expired",
        status=CustomerStatus.SUSPENDED.value,
        suspended_until=_YESTERDAY,
        cognito_sub="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    still_future = _account(
        id="future",
        status=CustomerStatus.SUSPENDED.value,
        suspended_until=_TOMORROW,
        cognito_sub="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    service, repo, cognito = _service([expired, still_future])

    lifted = service.run_suspension_sweep(now=_NOW)

    assert lifted == 1
    assert repo.accounts["expired"].status == CustomerStatus.ACTIVE.value
    assert repo.accounts["future"].status == CustomerStatus.SUSPENDED.value
    assert cognito.enable_calls == [expired.cognito_sub]
    assert repo.update_calls[0]["actor_admin_id"] == "system:suspension-sweep"


def test_suspension_sweep_continues_past_one_accounts_failure():
    a = _account(id="a", status=CustomerStatus.SUSPENDED.value, suspended_until=_YESTERDAY)
    b = _account(
        id="b",
        status=CustomerStatus.SUSPENDED.value,
        suspended_until=_YESTERDAY,
        cognito_sub="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    repo = FakeCustomerRepository([a, b])
    cognito = FakeCognitoAttributes(enable_raises=ExternalServiceUnavailableError("down"))
    service = CustomerStatusService(repo, cognito)

    lifted = service.run_suspension_sweep(now=_NOW)

    # Both accounts' Cognito call fails (same fake raises for every call),
    # so lifted count is 0, but the sweep must not crash/raise — both
    # rows were attempted independently.
    assert lifted == 0
    assert len(repo.update_calls) == 2


# --- Code-review fix: FR-7 sweep drift-reconciliation pass ---


def test_suspension_sweep_retries_a_pending_deactivated_account():
    account = _account(
        status=CustomerStatus.DEACTIVATED.value, cognito_sync_pending=True
    )
    service, repo, cognito = _service([account])

    service.run_suspension_sweep(now=_NOW)

    assert cognito.disable_calls == [account.cognito_sub]
    assert repo.accounts["cust-1"].cognito_sync_pending is False


def test_suspension_sweep_retries_a_pending_active_account():
    # A reactivate()'s AdminEnableUser can drift too -- the
    # reconciliation pass isn't limited to Suspended/Deactivated.
    account = _account(
        status=CustomerStatus.ACTIVE.value, cognito_sync_pending=True
    )
    service, repo, cognito = _service([account])

    service.run_suspension_sweep(now=_NOW)

    assert cognito.enable_calls == [account.cognito_sub]
    assert repo.accounts["cust-1"].cognito_sync_pending is False


def test_suspension_sweep_leaves_a_still_failing_account_pending():
    account = _account(
        status=CustomerStatus.DEACTIVATED.value, cognito_sync_pending=True
    )
    repo = FakeCustomerRepository([account])
    cognito = FakeCognitoAttributes(disable_raises=ExternalServiceUnavailableError("down"))
    service = CustomerStatusService(repo, cognito)

    service.run_suspension_sweep(now=_NOW)  # must not raise

    assert repo.accounts["cust-1"].cognito_sync_pending is True


def test_suspension_sweep_does_not_touch_clean_accounts():
    account = _account(status=CustomerStatus.ACTIVE.value, cognito_sync_pending=False)
    service, repo, cognito = _service([account])

    service.run_suspension_sweep(now=_NOW)

    assert cognito.enable_calls == []
    assert cognito.disable_calls == []
