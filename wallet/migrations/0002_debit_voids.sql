-- MA-142 FR-2: the debit void. Applies on top of 0001_wallets_ledger.sql.
-- The SQLAlchemy Core table in src/adapters/wallet_repository.py must stay
-- column-for-column compatible with this.
--
-- A row here is a fence: once it exists, `POST /wallet/internal/debit`
-- refuses `order:{orderId}` with 409 DEBIT_VOIDED. The void and the debit
-- both take the user's wallet row lock first, so for every ref at most one
-- of {ledger_entries row, debit_voids row} ever exists. Voids are not money
-- movements: they stay out of the ledger and the passbook.

CREATE TABLE debit_voids (
    ref        VARCHAR(80) PRIMARY KEY,   -- 'order:{orderId}', same namespace as ledger_entries.ref
    user_id    VARCHAR(64) NOT NULL,
    voided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
