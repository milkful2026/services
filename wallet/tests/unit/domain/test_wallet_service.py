import jsonschema
import pytest
from shared.events import load_schema

from adapters.wallet_repository import debit_voids_table, ledger_entries_table, wallets_table
from domain.exceptions import (
    AlreadyDebitedError,
    DebitNotFoundError,
    DebitVoidedError,
    InvalidAmountError,
    InvalidCursorError,
    InvalidTransactionTypeError,
    OrderUserMismatchError,
    RefundDebitNotFoundError,
    RefundExceedsDebitError,
    RefundOrderUserMismatchError,
    RetryableConsumerError,
    ServiceUnavailableError,
    WalletNotFoundError,
    WalletProvisioningPendingError,
)
from domain.models import DebitResult, LedgerType
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


def _mixed_ledger(service, engine):
    """OPENING + 3 RECHARGE + 5 ORDER_DEBIT, interleaved by id."""
    seed_wallet(engine, balance_paise=1_000_000)
    for i in range(3):
        service.credit_recharge(
            _payment_confirmed(razorpayPaymentId=f"rzp_{i}", paymentId=f"pay_{i}")
        )
        service.debit_for_order(
            user_id="user-1", order_id=f"ord_a{i}", amount_paise=1000, correlation_id="c"
        )
    for i in range(2):
        service.debit_for_order(
            user_id="user-1", order_id=f"ord_b{i}", amount_paise=1000, correlation_id="c"
        )


class TestTransactionTypeFilter:
    """MA-148 — optional `types` filter on the passbook."""

    def test_parse_types_trims_and_collapses(self):
        from domain.wallet_service import _parse_types

        assert _parse_types(None) is None
        assert _parse_types(" RECHARGE , RECHARGE ") == frozenset({LedgerType.RECHARGE})
        assert _parse_types("ORDER_DEBIT,REFUND") == frozenset(
            {LedgerType.ORDER_DEBIT, LedgerType.REFUND}
        )

    @pytest.mark.parametrize(
        "raw, invalid",
        [("", [""]), ("RECHARGE,,X", ["", "X"]), ("recharge", ["recharge"]), ("BOGUS", ["BOGUS"])],
    )
    def test_parse_types_rejects(self, raw, invalid):
        from domain.wallet_service import _parse_types

        with pytest.raises(InvalidTransactionTypeError) as exc:
            _parse_types(raw)
        assert exc.value.details == {"field": "types", "invalid": invalid}

    def test_filter_returns_only_matching_newest_first(self, service, engine):
        _mixed_ledger(service, engine)
        page = service.list_transactions("user-1", limit=10, cursor=None, types="RECHARGE")
        assert [e.type for e in page.items] == [LedgerType.RECHARGE] * 3
        assert [e.ref for e in page.items] == [
            "razorpay_payment:rzp_2",
            "razorpay_payment:rzp_1",
            "razorpay_payment:rzp_0",
        ]
        assert page.next_cursor is None

    def test_filtered_paging_is_full_and_gap_free(self, service, engine):
        _mixed_ledger(service, engine)
        first = service.list_transactions("user-1", limit=2, cursor=None, types="RECHARGE")
        assert len(first.items) == 2 and first.next_cursor is not None
        second = service.list_transactions(
            "user-1", limit=2, cursor=first.next_cursor, types="RECHARGE"
        )
        assert [e.ref for e in second.items] == ["razorpay_payment:rzp_0"]
        assert second.next_cursor is None

    def test_several_types_and_absent_type(self, service, engine):
        _mixed_ledger(service, engine)
        both = service.list_transactions(
            "user-1", limit=20, cursor=None, types="ORDER_DEBIT,RECHARGE"
        )
        assert len(both.items) == 8
        ids = [e.id for e in both.items]
        assert ids == sorted(ids, reverse=True)
        none = service.list_transactions("user-1", limit=20, cursor=None, types="REFUND")
        assert none.items == [] and none.next_cursor is None

    def test_no_filter_is_unchanged(self, service, engine):
        _mixed_ledger(service, engine)
        page = service.list_transactions("user-1", limit=20, cursor=None)
        assert len(page.items) == 9  # opening included

    def test_bad_types_raise_before_any_read(self, service):
        # No wallet exists: a read would raise WalletNotFoundError instead.
        with pytest.raises(InvalidTransactionTypeError):
            service.list_transactions("nobody", limit=10, cursor=None, types="BOGUS")


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

    def test_replay_after_wallet_deactivated_still_returns_debited(self, service, repo, engine):
        # Regression: the ref-replay check must run before the wallet
        # status check, or a replay of an already-debited order after
        # the wallet's status later changes would wrongly return
        # WALLET_NOT_ACTIVE instead of the idempotent DEBITED outcome.
        seed_wallet(engine, balance_paise=100000)
        first = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert first.result == DebitResult.DEBITED
        with engine.begin() as conn:
            conn.execute(
                wallets_table.update()
                .where(wallets_table.c.user_id == "user-1")
                .values(status="FAILED")
            )
        second = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id=None
        )
        assert second.result == DebitResult.DEBITED
        assert second.balance_paise == first.balance_paise

    def test_replayed_debit_does_not_emit_duplicate_low_balance(self, service, repo, engine):
        seed_wallet(engine, balance_paise=15000)  # threshold default is 10000
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=10000, correlation_id=None
        )
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=10000, correlation_id=None
        )
        low_balance = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletLowBalance"]
        assert len(low_balance) == 1

    def test_low_balance_enqueue_failure_does_not_fail_the_debit(
        self, service, repo, engine, monkeypatch
    ):
        # Regression: a transient failure in the best-effort
        # WalletLowBalance enqueue (a separate transaction from the
        # debit itself) must not surface as a failed debit_for_order —
        # the debit already committed.
        seed_wallet(engine, balance_paise=15000)  # threshold default is 10000

        def _boom(*args, **kwargs):
            raise ServiceUnavailableError("transient enqueue failure")

        monkeypatch.setattr(repo, "enqueue_outbox_event", _boom)

        outcome = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=10000, correlation_id=None
        )
        assert outcome.result == DebitResult.DEBITED
        assert outcome.balance_paise == 5000

    def test_debited_outbox_correlation_id_defaults_when_caller_omits_it(
        self, service, repo, engine
    ):
        # Regression: WalletDebited.schema.json requires a non-empty
        # correlationId; publishing "" for an omitted correlation_id
        # violates the schema this same PR added.
        seed_wallet(engine, balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=1000, correlation_id=None
        )
        debited = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletDebited"]
        assert len(debited) == 1
        assert debited[0]["payload"]["correlationId"] != ""
        assert len(debited[0]["payload"]["correlationId"]) > 0

    def test_ref_race_across_wallets_recovers_via_integrity_error_not_503(
        self, service, repo, engine, monkeypatch
    ):
        # Regression: the `ref` UNIQUE constraint (not the per-wallet
        # `FOR UPDATE` lock) is what actually serializes two concurrent
        # debit_for_order calls for the same order_id across *different*
        # wallets. Simulate the race by making the first ledger_entries
        # SELECT inside the transaction (the "is this ref already
        # debited" check) miss a row that a concurrent call already
        # committed, so the subsequent INSERT hits the UNIQUE constraint.
        # The repository must recover with the same OrderUserMismatchError
        # the pre-insert check would have raised, not a raw 503.
        seed_wallet(engine, user_id="user-1", wallet_id="wal_1", balance_paise=100000)
        seed_wallet(engine, user_id="user-2", wallet_id="wal_2", balance_paise=100000)

        service.debit_for_order(
            user_id="user-2", order_id="order-1", amount_paise=1000, correlation_id=None
        )

        from sqlalchemy.engine import Connection

        real_execute = Connection.execute
        state = {"skipped": False}

        class _EmptyResult:
            def fetchone(self):
                return None

        def patched_execute(self, statement, *args, **kwargs):
            if (
                not state["skipped"]
                and getattr(statement, "is_select", False)
                and "ledger_entries" in str(statement)
            ):
                state["skipped"] = True
                return _EmptyResult()
            return real_execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "execute", patched_execute)

        with pytest.raises(OrderUserMismatchError):
            service.debit_for_order(
                user_id="user-1", order_id="order-1", amount_paise=2000, correlation_id=None
            )

        monkeypatch.undo()
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000


class TestGetDebitForOrder:
    """MA-142 — read-only debit lookup by order id."""

    def test_debited_order_returns_positive_amount(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
        )
        debit = service.get_debit_for_order("order-1")
        assert debit["orderId"] == "order-1"
        assert debit["status"] == "DEBITED"
        assert debit["amountPaise"] == 30000
        assert debit["balanceAfterPaise"] == 70000
        assert debit["walletId"] == "wal_1"
        assert debit["debitedAt"]

    def test_unknown_order_raises_not_found(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        with pytest.raises(DebitNotFoundError):
            service.get_debit_for_order("order-never")

    def test_lookup_does_not_change_balance(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
        )
        service.get_debit_for_order("order-1")
        service.get_debit_for_order("order-1")
        assert repo.get_wallet_by_user("user-1").balance_paise == 70000

    def test_non_debit_entry_on_ref_is_not_found_and_logged(self, service, engine, caplog):
        seed_wallet(engine, balance_paise=100000)
        with engine.begin() as conn:
            conn.execute(
                ledger_entries_table.insert().values(
                    wallet_id="wal_1",
                    type="ADJUSTMENT",
                    amount_paise=100,
                    balance_after_paise=100100,
                    ref="order:odd",
                )
            )
        with caplog.at_level("ERROR"), pytest.raises(DebitNotFoundError):
            service.get_debit_for_order("odd")
        assert "non-debit ledger entry" in caplog.text

    def test_voided_order_returns_voided(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        voided = service.void_debit_for_order("user-1", "order-1")
        assert service.get_debit_for_order("order-1") == voided


class TestVoidDebitForOrder:
    """MA-142 FR-2/FR-3 — the void fences an order against any later debit."""

    def test_void_without_debit_records_a_void(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        body = service.void_debit_for_order("user-1", "order-1")
        assert body["orderId"] == "order-1"
        assert body["status"] == "VOIDED"
        assert body["voidedAt"]
        with engine.connect() as conn:
            rows = conn.execute(debit_voids_table.select()).fetchall()
        assert [(r.ref, r.user_id) for r in rows] == [("order:order-1", "user-1")]
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000

    def test_void_after_debit_returns_the_debit_and_writes_nothing(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
        )
        with pytest.raises(AlreadyDebitedError) as exc_info:
            service.void_debit_for_order("user-1", "order-1")
        details = exc_info.value.details
        assert details["status"] == "DEBITED"
        assert details["amountPaise"] == 30000  # positive, though the ledger stores -30000
        assert details["balanceAfterPaise"] == 70000
        with engine.connect() as conn:
            assert conn.execute(debit_voids_table.select()).fetchall() == []
        assert repo.get_wallet_by_user("user-1").balance_paise == 70000

    def test_void_twice_returns_the_same_voided_at(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        first = service.void_debit_for_order("user-1", "order-1")
        assert service.void_debit_for_order("user-1", "order-1") == first

    def test_debit_after_void_is_refused_and_balance_unchanged(
        self, service, repo, engine, caplog
    ):
        seed_wallet(engine, balance_paise=100000)
        voided = service.void_debit_for_order("user-1", "order-1")
        with caplog.at_level("WARNING"), pytest.raises(DebitVoidedError) as exc_info:
            service.debit_for_order(
                user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
            )
        assert exc_info.value.details == {"orderId": "order-1", "voidedAt": voided["voidedAt"]}
        assert "wallet.debit_refused_voided" in caplog.text
        wallet = repo.get_wallet_by_user("user-1")
        assert wallet.balance_paise == 100000
        assert [e.type.value for e in repo.list_ledger_entries(wallet.id, 10, None)] == [
            "OPENING"
        ]

    def test_debit_replay_after_commit_still_replays_and_void_sees_it(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
        )
        with pytest.raises(AlreadyDebitedError):
            service.void_debit_for_order("user-1", "order-1")
        replay = service.debit_for_order(
            user_id="user-1", order_id="order-1", amount_paise=30000, correlation_id="c"
        )
        assert replay.result == DebitResult.DEBITED
        assert replay.replayed

    def test_void_for_user_without_wallet_is_voided(self, service, engine):
        assert service.void_debit_for_order("ghost", "order-1")["status"] == "VOIDED"

    def test_void_ignores_wallet_status(self, service, engine):
        seed_wallet(engine, balance_paise=100000, status="FAILED")
        assert service.void_debit_for_order("user-1", "order-1")["status"] == "VOIDED"


class TestRefundForOrder:
    """MA-153 FR-2 — credit an order's debit back, exactly once."""

    @staticmethod
    def _debit(service, order_id="ord_1", amount=30000, user_id="user-1"):
        service.debit_for_order(
            user_id=user_id, order_id=order_id, amount_paise=amount, correlation_id="c"
        )

    @staticmethod
    def _refund(service, order_id="ord_1", amount=30000, refund_id="cancel", user_id="user-1"):
        return service.refund_for_order(
            user_id=user_id,
            order_id=order_id,
            refund_id=refund_id,
            amount_paise=amount,
            correlation_id="corr-r",
        )

    @staticmethod
    def _refund_entries(repo, user_id="user-1"):
        w = repo.get_wallet_by_user(user_id)
        return [
            e for e in repo.list_ledger_entries(w.id, 50, None) if e.type == LedgerType.REFUND
        ]

    def test_refund_restores_balance_and_emits_walletrefunded(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        outcome = self._refund(service)

        assert outcome.replayed is False
        assert outcome.amount_paise == 30000
        assert outcome.balance_after_paise == 100000
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000
        [entry] = self._refund_entries(repo)
        assert entry.ref == "refund:ord_1:cancel"
        assert entry.amount_paise == 30000
        assert outcome.ledger_entry_id == entry.id

        events = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletRefunded"]
        assert len(events) == 1
        payload = events[0]["payload"]
        jsonschema.validate(payload, load_schema("WalletRefunded"))
        assert payload["ref"] == "refund:ord_1:cancel"
        assert payload["correlationId"] == "corr-r"

    def test_replay_returns_the_original_entry_and_writes_nothing(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        first = self._refund(service)
        second = self._refund(service)

        assert second.replayed is True
        assert second.ledger_entry_id == first.ledger_entry_id
        assert second.balance_after_paise == first.balance_after_paise
        assert len(self._refund_entries(repo)) == 1
        refunded = [e for e in repo.fetch_unpublished() if e["event_type"] == "WalletRefunded"]
        assert len(refunded) == 1
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000

    def test_no_debit_is_debit_not_found_409(self, service, engine):
        seed_wallet(engine, balance_paise=100000)
        with pytest.raises(RefundDebitNotFoundError) as exc:
            self._refund(service)
        assert isinstance(exc.value, DebitNotFoundError)
        assert exc.value.http_status == 409

    def test_voided_order_is_debit_not_found(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        service.void_debit_for_order("user-1", "ord_1")
        with pytest.raises(RefundDebitNotFoundError):
            self._refund(service)
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000

    def test_over_refund_is_refused_with_details(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        self._refund(service)
        with pytest.raises(RefundExceedsDebitError) as exc:
            self._refund(service, amount=1, refund_id="extra")
        assert exc.value.http_status == 409
        assert exc.value.details == {"debitedPaise": 30000, "alreadyRefundedPaise": 30000}
        assert len(self._refund_entries(repo)) == 1

    def test_partial_refunds_up_to_exactly_the_debit_are_allowed(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        self._refund(service, amount=10000, refund_id="part1")
        self._refund(service, amount=20000, refund_id="part2")
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000

    def test_cap_matches_the_order_prefix_literally(self, service, repo, engine):
        # `_` must not act as a LIKE wildcard: ordX1's refund isn't ord_1's.
        seed_wallet(engine, balance_paise=100000)
        self._debit(service, order_id="ordX1", amount=30000)
        self._refund(service, order_id="ordX1", amount=30000)
        self._debit(service, order_id="ord_1", amount=30000)
        outcome = self._refund(service, order_id="ord_1", amount=30000)
        assert outcome.replayed is False
        assert len(self._refund_entries(repo)) == 2

    @pytest.mark.parametrize("amount", [0, -1])
    def test_non_positive_amount_is_invalid(self, service, engine, amount):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        with pytest.raises(InvalidAmountError):
            self._refund(service, amount=amount)

    def test_failed_wallet_is_still_credited(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        self._debit(service)
        with engine.begin() as conn:
            conn.execute(wallets_table.update().values(status="FAILED"))
        self._refund(service)
        assert repo.get_wallet_by_user("user-1").balance_paise == 100000

    def test_other_users_debit_is_a_409_mismatch(self, service, repo, engine):
        seed_wallet(engine, balance_paise=100000)
        seed_wallet(engine, user_id="user-2", wallet_id="wal_2", balance_paise=0)
        self._debit(service)
        with pytest.raises(RefundOrderUserMismatchError) as exc:
            self._refund(service, user_id="user-2")
        assert isinstance(exc.value, OrderUserMismatchError)
        assert exc.value.http_status == 409
        assert repo.get_wallet_by_user("user-2").balance_paise == 0

    def test_no_wallet_is_not_found(self, service):
        with pytest.raises(WalletNotFoundError):
            self._refund(service, user_id="ghost")
