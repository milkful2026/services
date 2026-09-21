"""Wallet domain service — the only place business rules live.

Covers the MA-1 auto-provision baseline (create_wallet from
UserRegistered, GET /wallet/me/status, retry) and the MA-24 recharge
slice (GET /wallet/me, GET /wallet/me/transactions, credit_recharge from
PaymentConfirmed, GET /wallet/internal/limits).
"""

import logging
import uuid
from datetime import UTC, datetime

from adapters.wallet_repository import (
    SqlAlchemyWalletRepository,
    decode_cursor,
    encode_cursor,
    new_wallet_id,
)
from config.env import Settings
from domain.exceptions import (
    InvalidAmountError,
    InvalidCursorError,
    WalletError,
    WalletNotFoundError,
)
from domain.models import (
    DebitOutcome,
    DebitResult,
    LedgerEntry,
    LedgerType,
    TransactionsPage,
    Wallet,
    WalletStatus,
)

logger = logging.getLogger(__name__)

_MAX_PAGE = 100
_DEFAULT_PAGE = 20

_DESCRIPTIONS = {
    LedgerType.OPENING: "Wallet created",
    LedgerType.RECHARGE: "Wallet top-up",
    LedgerType.ORDER_DEBIT: "Order payment",
    LedgerType.REFUND: "Refund",
    LedgerType.CASHBACK: "Cashback",
    LedgerType.REFERRAL_CREDIT: "Referral credit",
    LedgerType.ADJUSTMENT: "Adjustment",
}


class WalletService:
    def __init__(self, repository: SqlAlchemyWalletRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings

    # --- MA-1 baseline ---

    def create_wallet(self, user_registered: dict) -> None:
        """Consume a UserRegistered event. Idempotent on user_id."""
        user_id = user_registered["userId"]
        created = self._repo.insert_wallet_if_absent(new_wallet_id(), user_id)
        if not created:
            logger.info("create_wallet: wallet already exists, no-op", extra={"userId": user_id})
            return
        logger.info("create_wallet: wallet provisioned", extra={"userId": user_id})

    def retry_provision(self, user_id: str) -> None:
        """MA-1 POST /wallet/me/retry — replay the create by user id."""
        self.create_wallet({"userId": user_id})

    def get_wallet_status_legacy(self, user_id: str) -> dict:
        """MA-1's GET /wallet/me/status — UNCHANGED body:
        {walletId, status, balance (whole rupees), currency}. Not the
        MA-24 balancePaise shape (round-2 finding #9)."""
        wallet = self._repo.get_wallet_by_user(user_id)
        if wallet is None:
            return {
                "walletId": None,
                "status": WalletStatus.CREATING.value,
                "balance": 0,
                "currency": "INR",
            }
        return {
            "walletId": wallet.id,
            "status": wallet.status.value,
            "balance": wallet.balance_paise // 100,
            "currency": wallet.currency,
        }

    # --- MA-24 read APIs ---

    def get_wallet_me(self, user_id: str) -> dict:
        """MA-24 GET /wallet/me — the new endpoint MA-125 consumes."""
        wallet = self._repo.get_wallet_by_user(user_id)
        if wallet is None:
            return {
                "walletId": None,
                "status": WalletStatus.CREATING.value,
                "balancePaise": 0,
                "currency": "INR",
                "rechargeMinPaise": self._settings.recharge_min_paise,
                "rechargeMaxPaise": self._settings.recharge_max_paise,
            }
        return {
            "walletId": wallet.id,
            "status": wallet.status.value,
            "balancePaise": wallet.balance_paise,
            "currency": wallet.currency,
            "rechargeMinPaise": self._settings.recharge_min_paise,
            "rechargeMaxPaise": self._settings.recharge_max_paise,
        }

    def get_internal_limits(self) -> dict:
        return {
            "rechargeMinPaise": self._settings.recharge_min_paise,
            "rechargeMaxPaise": self._settings.recharge_max_paise,
        }

    def get_internal_balance(self, user_id: str) -> dict:
        """MA-130 FR-3 — service-to-service balance read (SigV4/mTLS in
        prod, VPC-only). Same CREATING-if-absent convention as get_wallet_me."""
        wallet = self._repo.get_wallet_by_user(user_id)
        if wallet is None:
            return {"balancePaise": 0, "status": WalletStatus.CREATING.value}
        return {"balancePaise": wallet.balance_paise, "status": wallet.status.value}

    def list_transactions(
        self, user_id: str, limit: int | None, cursor: str | None
    ) -> TransactionsPage:
        wallet = self._repo.get_wallet_by_user(user_id)
        if wallet is None:
            raise WalletNotFoundError("No wallet for this account")

        page_size = _DEFAULT_PAGE if not limit else max(1, min(limit, _MAX_PAGE))
        before_id = None
        if cursor:
            try:
                before_id = decode_cursor(cursor)
            except Exception as exc:  # noqa: BLE001 — any decode failure is a bad cursor
                raise InvalidCursorError("Malformed pagination cursor") from exc

        entries = self._repo.list_ledger_entries(wallet.id, page_size + 1, before_id)
        has_more = len(entries) > page_size
        entries = entries[:page_size]
        next_cursor = encode_cursor(entries[-1].id) if (has_more and entries) else None
        return TransactionsPage(items=entries, next_cursor=next_cursor)

    # --- MA-24 recharge consumer ---

    def credit_recharge(self, payment_confirmed: dict) -> None:
        """Consume PaymentConfirmed(purpose=WALLET_RECHARGE). Idempotent
        on razorpay_payment_id via the ledger `ref` UNIQUE."""
        user_id = payment_confirmed["userId"]
        amount_paise = int(payment_confirmed["amountPaise"])
        currency = payment_confirmed.get("currency", "INR")
        if currency != "INR":
            raise ValueError(f"unsupported currency: {currency!r}")
        correlation_id = payment_confirmed.get("correlationId")
        rzp_payment_id = payment_confirmed["razorpayPaymentId"]
        payment_id = payment_confirmed["paymentId"]
        ref = f"razorpay_payment:{rzp_payment_id}"

        if not (
            self._settings.recharge_min_paise
            <= amount_paise
            <= self._settings.recharge_max_paise
        ):
            # Never reject real money — the capture already happened.
            logger.warning(
                "credit_recharge: amount outside current limits, crediting anyway",
                extra={"userId": user_id, "amountPaise": amount_paise},
            )

        def _build_outbox(wallet: Wallet, balance_after_paise: int) -> dict:
            return {
                "eventId": str(uuid.uuid4()),
                "occurredAt": datetime.now(UTC).isoformat(),
                "correlationId": correlation_id or "",
                "userId": user_id,
                "walletId": wallet.id,
                "amountPaise": amount_paise,
                "balanceAfterPaise": balance_after_paise,
                "type": "RECHARGE",
                "ref": ref,
                "paymentId": payment_id,
            }

        result = self._repo.credit_recharge(
            user_id=user_id,
            amount_paise=amount_paise,
            ref=ref,
            correlation_id=correlation_id,
            outbox_payload_builder=_build_outbox,
        )
        if result is None:
            logger.info(
                "credit_recharge: duplicate PaymentConfirmed, no-op",
                extra={"ref": ref},
            )
        else:
            logger.info(
                "credit_recharge: wallet credited",
                extra={"ref": ref, "balanceAfterPaise": result.balance_paise},
            )


    # --- MA-130 (MA-25): synchronous debit for Order Service ---

    def debit_for_order(
        self, *, user_id: str, order_id: str, amount_paise: int, correlation_id: str | None
    ) -> DebitOutcome:
        """`POST /wallet/internal/debit` — Order Service's synchronous
        order-creation critical path. Idempotent on `order_id` via the
        ledger `ref` UNIQUE (a replayed call for an already-debited order
        returns the same DEBITED outcome, no second write). Raises
        `InvalidAmountError` for a non-positive amount, and
        `WalletProvisioningPendingError`/`OrderUserMismatchError` per
        `debit_for_order`'s own contract on the repository — both are
        real exceptions (never a `DebitOutcome`), since both are either
        a caller bug or a race the caller must retry, not an outcome
        Order Service should branch on."""
        if amount_paise <= 0:
            raise InvalidAmountError("amountPaise must be > 0")

        ref = f"order:{order_id}"

        def _build_debited_outbox(wallet_id: str, balance_after_paise: int) -> dict:
            return {
                "eventId": str(uuid.uuid4()),
                "occurredAt": datetime.now(UTC).isoformat(),
                # WalletDebited.schema.json requires a non-empty
                # correlationId; DebitRequest.correlationId is optional
                # from the caller, so mint one rather than publish "".
                "correlationId": correlation_id or str(uuid.uuid4()),
                "userId": user_id,
                "walletId": wallet_id,
                "orderId": order_id,
                "amountPaise": amount_paise,
                "balanceAfterPaise": balance_after_paise,
            }

        outcome = self._repo.debit_for_order(
            user_id=user_id,
            order_id=order_id,
            amount_paise=amount_paise,
            ref=ref,
            correlation_id=correlation_id,
            outbox_payload_builder=_build_debited_outbox,
        )

        # Skip on replay: the ledger/balance write already happened (and
        # was already evaluated for low-balance) on the original call —
        # re-running this on every retry would emit a duplicate
        # WalletLowBalance per replay, each with its own eventId (so
        # consumer-side eventId dedup wouldn't catch it either).
        if not outcome.replayed:
            try:
                self._maybe_enqueue_low_balance(user_id, outcome)
            except WalletError:
                # Best-effort secondary write; the debit itself already
                # committed, so a failure here must not surface as a
                # failed debit_for_order call (e.g. a 503 to Order
                # Service for money that was, in fact, taken).
                logger.error(
                    "debit_for_order: failed to enqueue WalletLowBalance after a "
                    "successful debit",
                    extra={"userId": user_id, "orderId": order_id},
                )

        logger.info(
            "debit_for_order: %s",
            outcome.result.value,
            extra={"userId": user_id, "orderId": order_id, "result": outcome.result.value},
        )
        return outcome

    def _maybe_enqueue_low_balance(self, user_id: str, outcome: DebitOutcome) -> None:
        """After a DEBITED or INSUFFICIENT_BALANCE outcome (never
        WALLET_NOT_ACTIVE — no balance to compare), enqueue
        WalletLowBalance if the resulting/refused-against balance is
        under threshold. A separate, best-effort outbox insert — not
        part of debit_for_order's own transaction, same as this
        codebase's other post-commit side-effect enqueues."""
        if outcome.result not in (DebitResult.DEBITED, DebitResult.INSUFFICIENT_BALANCE):
            return
        if outcome.balance_paise is None or outcome.wallet_id is None:
            return
        if outcome.balance_paise >= self._settings.low_balance_threshold_paise:
            return
        reason = "LOW_AFTER_DEBIT" if outcome.result == DebitResult.DEBITED else "DEBIT_REFUSED"
        self._repo.enqueue_outbox_event(
            aggregate_id=outcome.wallet_id,
            event_type="WalletLowBalance",
            payload={
                "eventId": str(uuid.uuid4()),
                "occurredAt": datetime.now(UTC).isoformat(),
                "userId": user_id,
                "walletId": outcome.wallet_id,
                "balancePaise": outcome.balance_paise,
                "thresholdPaise": self._settings.low_balance_threshold_paise,
                "reason": reason,
            },
        )

    # --- MA-127 §5/§7/§11: nightly balance invariant ---

    def check_balance_invariant(self) -> list[str]:
        """Asserts `wallets.balance_paise == SUM(ledger_entries.amount_paise)`
        for every wallet. A mismatch is logged as the
        `wallet.balance_invariant_violations` metric per offending wallet
        (CloudWatch metric filter on this log line, matching this
        codebase's existing logging-only convention — no service here
        has a separate metrics client) rather than raised, so one bad
        wallet doesn't abort the sweep. Returns the offending wallet ids."""
        violations = self._repo.find_balance_invariant_violations()
        offending_ids: list[str] = []
        for wallet_id, balance_paise, ledger_sum_paise in violations:
            offending_ids.append(wallet_id)
            logger.error(
                "wallet.balance_invariant_violations",
                extra={
                    "metric": "wallet.balance_invariant_violations",
                    "walletId": wallet_id,
                    "balancePaise": balance_paise,
                    "ledgerSumPaise": ledger_sum_paise,
                },
            )
        return offending_ids


def render_description(entry: LedgerEntry) -> str:
    return _DESCRIPTIONS.get(entry.type, entry.type.value)
