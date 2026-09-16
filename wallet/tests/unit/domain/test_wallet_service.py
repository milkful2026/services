import pytest

from adapters.wallet_repository import ledger_entries_table, wallets_table
from domain.exceptions import InvalidCursorError, RetryableConsumerError, WalletNotFoundError
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
