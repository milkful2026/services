import pytest

from adapters.wallet_repository import ledger_entries_table, wallets_table
from domain.exceptions import (
    InvalidAmountError,
    InvalidCursorError,
    OrderUserMismatchError,
    RetryableConsumerError,
    WalletNotFoundError,
    WalletProvisioningPendingError,
)
from domain.models import DebitResult
from tests.conftest import seed_wallet


def _payment_confirmed(**overrides):
    detail = {
        "eventId": "evt-1",
        "occurredAt": "2026-09-11T10:00:00+00:00",
        "correlationId": "corr-1",
        "paymentId": "pay_1",
        "userId": "user-1",
        "purpose": "WALLET_RECHARGE",
        "amountPaise": 50000,
        "currency": "INR",
        "method": "UPI",
        "razorpayPaymentId": "rzp_pay_1",
        "razorpayOrderId": "rzp_ord_1",
    }
    detail.update(overrides)
    return detail


class TestCreateWallet:
    def test_creates_wallet_and_opening_entry(self, service, repo):
        service.create_wallet({"userId": "user-1"})
        w = repo.get_wallet_by_user("user-1")
        assert w is not None
        assert w.balance_paise == 0
        assert w.status.value == "ACTIVE"
        entries = repo.list_ledger_entries(w.id, 10, None)
        assert [e.type.value for e in entries] == ["OPENING"]
        assert entries[0].ref == f"opening:{w.id}"

    def test_idempotent_on_user_id(self, service, repo):
        service.create_wallet({"userId": "user-1"})
        first = repo.get_wallet_by_user("user-1")
        service.create_wallet({"userId": "user-1"})
        second = repo.get_wallet_by_user("user-1")
        assert first.id == second.id
        assert len(repo.list_ledger_entries(first.id, 10, None)) == 1


class TestReadApis:
    def test_get_wallet_me_shape_with_bounds(self, service, engine):
        seed_wallet(engine, balance_paise=45000)
        body = service.get_wallet_me("user-1")
        assert body == {
            "walletId": "wal_1",
            "status": "ACTIVE",
            "balancePaise": 45000,
            "currency": "INR",
            "rechargeMinPaise": 10000,
            "rechargeMaxPaise": 10000000,
        }

    def test_get_wallet_me_missing_wallet_is_creating(self, service):
        body = service.get_wallet_me("nobody")
        assert body["status"] == "CREATING"
        assert body["balancePaise"] == 0
        assert body["rechargeMinPaise"] == 10000

    def test_status_legacy_is_rupees_and_has_no_balance_paise_key(self, service, engine):
        seed_wallet(engine, balance_paise=45000)
        body = service.get_wallet_status_legacy("user-1")
        assert body == {
            "walletId": "wal_1",
            "status": "ACTIVE",
            "balance": 450,
            "currency": "INR",
        }
        assert "balancePaise" not in body
        assert "rechargeMinPaise" not in body

    def test_internal_limits(self, service):
        assert service.get_internal_limits() == {
            "rechargeMinPaise": 10000,
            "rechargeMaxPaise": 10000000,
        }

    def test_list_transactions_newest_first_and_cursor(self, service, repo, engine):
        seed_wallet(engine)
        for i in range(3):
            service.credit_recharge(
                _payment_confirmed(
                    razorpayPaymentId=f"rzp_{i}", paymentId=f"pay_{i}", amountPaise=10000
                )
            )
        page = service.list_transactions("user-1", limit=2, cursor=None)
        assert len(page.items) == 2
        assert page.items[0].type.value == "RECHARGE"
        assert page.next_cursor is not None
        page2 = service.list_transactions("user-1", limit=10, cursor=page.next_cursor)
        # remaining 2 (one more RECHARGE + the OPENING)
        assert [e.type.value for e in page2.items] == ["RECHARGE", "OPENING"]
        assert page2.next_cursor is None

    def test_list_transactions_bad_cursor(self, service, engine):
        seed_wallet(engine)
        with pytest.raises(InvalidCursorError):
            service.list_transactions("user-1", limit=10, cursor="!!!not-base64!!!")

    def test_list_transactions_no_wallet(self, service):
        with pytest.raises(WalletNotFoundError):
            service.list_transactions("nobody", limit=10, cursor=None)


class TestCreditRecharge:
    def test_fresh_credit(self, service, repo, engine):
        seed_wallet(engine, balance_paise=45000)
        service.credit_recharge(_payment_confirmed())
        w = repo.get_wallet_by_user("user-1")
        assert w.balance_paise == 95000
        entries = repo.list_ledger_entries(w.id, 10, None)
        rech = [e for e in entries if e.type.value == "RECHARGE"]
        assert len(rech) == 1
        assert rech[0].amount_paise == 50000
        assert rech[0].balance_after_paise == 95000
        assert rech[0].ref == "razorpay_payment:rzp_pay_1"
        unpub = repo.fetch_unpublished()
        assert len(unpub) == 1
        assert unpub[0]["event_type"] == "WalletCredited"
        assert unpub[0]["payload"]["amountPaise"] == 50000
        assert unpub[0]["payload"]["balanceAfterPaise"] == 95000

    def test_duplicate_event_is_noop(self, service, repo, engine):
        seed_wallet(engine, balance_paise=45000)
        service.credit_recharge(_payment_confirmed())
        service.credit_recharge(_payment_confirmed())  # same razorpayPaymentId
        w = repo.get_wallet_by_user("user-1")
        assert w.balance_paise == 95000  # credited once
        assert len(repo.fetch_unpublished()) == 1  # one WalletCredited

    def test_no_wallet_raises_retryable(self, service):
        with pytest.raises(RetryableConsumerError):
            service.credit_recharge(_payment_confirmed(userId="ghost"))

    def test_non_active_wallet_raises_retryable(self, service, engine):
        seed_wallet(engine, status="CREATING")
        with pytest.raises(RetryableConsumerError):
            service.credit_recharge(_payment_confirmed())

    def test_amount_outside_limits_still_credits(self, service, repo, engine):
        seed_wallet(engine, balance_paise=0)
        service.credit_recharge(_payment_confirmed(amountPaise=5000))  # below ₹100 min
        assert repo.get_wallet_by_user("user-1").balance_paise == 5000

    def test_non_inr_rejected(self, service, engine):
        seed_wallet(engine)
        with pytest.raises(ValueError):
            service.credit_recharge(_payment_confirmed(currency="USD"))


class TestBalanceInvariant:
    def test_consistent_ledger_reports_no_violations(self, service, repo, engine):
        # balance_paise=0 so the OPENING entry's amount_paise=0 keeps the
        # ledger sum consistent before the recharge is applied.
        seed_wallet(engine, balance_paise=0)
        service.credit_recharge(_payment_confirmed())
        assert service.check_balance_invariant() == []

    def test_mismatched_wallet_is_reported(self, service, engine, repo):
        seed_wallet(engine, balance_paise=999999)  # doesn't match the ledger's 0-sum opening entry
        offending = service.check_balance_invariant()
        assert offending == ["wal_1"]

    def test_mixed_entry_types_still_consistent(self, service, repo, engine):
        seed_wallet(engine, balance_paise=0)
        service.credit_recharge(_payment_confirmed(amountPaise=50000))
        with engine.begin() as conn:
            conn.execute(
                ledger_entries_table.insert().values(
                    wallet_id="wal_1",
                    type="ORDER_DEBIT",
                    amount_paise=-20000,
                    balance_after_paise=30000,
                    ref="order:debit-1",
                    correlation_id=None,
                )
            )
            conn.execute(
                wallets_table.update()
                .where(wallets_table.c.id == "wal_1")
                .values(balance_paise=30000)
            )
        assert service.check_balance_invariant() == []


class TestDebitForOrder:
    def test_sufficient_debits_and_emits_walletdebited(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        outcome = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="corr-1"
        )
        assert outcome.result == DebitResult.DEBITED
        assert outcome.balance_paise == 70000
        w = repo.get_wallet_by_user("user-1")
        assert w.balance_paise == 70000
        entries = repo.list_ledger_entries(w.id, 10, None)
        debit = [e for e in entries if e.type.value == "ORDER_DEBIT"]
        assert len(debit) == 1
        assert debit[0].amount_paise == -30000
        assert debit[0].balance_after_paise == 70000
        assert debit[0].ref == "order:order-1"
        unpub = repo.fetch_unpublished()
        debited_events = [e for e in unpub if e["event_type"] == "WalletDebited"]
        assert len(debited_events) == 1
        assert debited_events[0]["payload"]["amountPaise"] == 30000
        assert debited_events[0]["payload"]["balanceAfterPaise"] == 70000
        assert debited_events[0]["payload"]["orderId"] == "order-1"

    def test_insufficient_balance_no_write(self, service, repo, engine):
        # balance_paise above the default low-balance threshold (10000) so
        # this test isolates "no ledger/balance write" from the separate
        # low-balance-emission behavior covered below.
        seed_wallet(engine, balance_paise=15000)
        outcome = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert outcome.result == DebitResult.INSUFFICIENT_BALANCE
        assert outcome.balance_paise == 15000
        assert outcome.required_paise == 30000
        assert repo.get_wallet_by_user("user-1").balance_paise == 15000  # unchanged
        assert repo.fetch_unpublished() == []

    def test_wallet_not_active_no_write(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000, status="FAILED")
        outcome = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert outcome.result == DebitResult.WALLET_NOT_ACTIVE
        assert outcome.balance_paise is None
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000  # unchanged
        assert repo.fetch_unpublished() == []

    def test_no_wallet_row_raises_provisioning_pending_not_wallet_not_active(self, service):
        # Regression: a missing wallet row is a provisioning race (retryable,
        # 503) — it must NOT be folded into the 200 WALLET_NOT_ACTIVE outcome,
        # or Order Service would permanently fail a subscription's first-ever
        # order instead of retrying once provisioning catches up.
        with pytest.raises(WalletProvisioningPendingError):
            service.debit_for_order(
                user_id="ghost", order_id="order-1", amount_paise=30000, correlation_id=None
            )

    def test_replayed_debit_for_same_order_is_idempotent(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        first = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        second = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert first.result == second.result == DebitResult.DEBITED
        assert first.balance_paise == second.balance_paise == 70000
        # No second ledger row, no second WalletDebited.
        entries = repo.list_ledger_entries(repo.get_wallet_by_user("user-1").id, 10, None)
        assert len([e for e in entries if e.type.value == "ORDER_DEBIT"]) == 1
        assert len([e for e in repo.fetch_unpublished() if e["event_type"] == "WalletDebited"]) == 1

    def test_replayed_debit_for_different_user_raises_mismatch(self, service, engine):
        seed_wallet(engine, user_id="user-1", wallet_id="wal_1", balance_paise=100000)
        seed_wallet(engine, user_id="user-2", wallet_id="wal_2", balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        with pytest.raises(OrderUserMismatchError):
            service.debit_for_order(
                user_id="user-2", order_id="order-1", amount_paise=30000, correlation_id=None
            )

    def test_first_ever_call_for_new_order_never_raises_mismatch(self, service, engine):
        # Regression: the mismatch check must never fire on a brand-new
        # order_id — only on a genuine replay against a different wallet.
        seed_wallet(engine, balance_paise=100000)
        outcome = service.debit_for_order(
            user_id="user-1", order_id="brand-new-order", amount_paise=100, correlation_id=None
        )
        assert outcome.result == DebitResult.DEBITED

    def test_non_positive_amount_raises_before_any_repository_call(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        with pytest.raises(InvalidAmountError):
            service.debit_for_order(
                user_id="user-1", order_id="order-1", amount_paise=0, correlation_id=None
            )

    def test_post_debit_balance_under_threshold_emits_low_balance(self, service, repo, engine):
        seed_wallet(engine, balance_paise=15000)  # threshold default is 10000
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=10000, correlation_id=None
        )
        low_balance = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletLowBalance"]
        assert len(low_balance) == 1
        assert low_balance[0]["payload"]["reason"] == "LOW_AFTER_DEBIT"
        assert low_balance[0]["payload"]["balancePaise"] == 5000

    def test_refused_debit_under_threshold_emits_low_balance_debit_refused(
        self, service, repo, engine
    ):
        seed_wallet(engine, balance_paise=5000)  # below threshold already, and insufficient
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        low_balance = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletLowBalance"]
        assert len(low_balance) == 1
        assert low_balance[0]["payload"]["reason"] == "DEBIT_REFUSED"

    def test_wallet_not_active_never_emits_low_balance(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100, status="FAILED")
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert repo.fetch_unpublished() == []
