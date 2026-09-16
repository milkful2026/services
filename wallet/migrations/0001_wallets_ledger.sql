-- Production-authoritative schema (Postgres/Aurora `wallet` cluster).
-- The SQLAlchemy Core tables in src/adapters/wallet_repository.py must
-- stay column-for-column compatible with this.
--
-- Money is stored as integer paise everywhere (BIGINT). No NUMERIC/float
-- for balances or amounts.
--
-- This is the MA-1 wallet-auto-provision baseline (wallets, ledger_entries,
-- opening entry) AND the MA-24 recharge extension (signed amount_paise,
-- balance_after_paise snapshot, UNIQUE ref for idempotent crediting,
-- correlation_id, outbox) in one migration — MA-1 was never implemented,
-- so there is no prior deployed schema to ALTER; it is created correct
-- from the start.

CREATE TABLE wallets (
    id            VARCHAR(64) PRIMARY KEY,
    user_id       VARCHAR(64) NOT NULL UNIQUE,
    balance_paise BIGINT NOT NULL DEFAULT 0 CHECK (balance_paise >= 0),
    currency      CHAR(3) NOT NULL DEFAULT 'INR',
    status        VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | CREATING | FAILED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_id           VARCHAR(64) NOT NULL REFERENCES wallets(id),
    -- OPENING | RECHARGE | ORDER_DEBIT | REFUND | CASHBACK | REFERRAL_CREDIT | ADJUSTMENT
    type                VARCHAR(24) NOT NULL,
    amount_paise        BIGINT NOT NULL,                    -- signed: +credit / -debit
    balance_after_paise BIGINT NOT NULL,                    -- running-balance snapshot for the passbook
    -- Dedupe key. For a recharge: 'razorpay_payment:<id>'. For the opening
    -- entry: 'opening:<wallet_id>'. UNIQUE makes the recharge credit
    -- idempotent (INSERT ... ON CONFLICT DO NOTHING).
    ref                 TEXT NOT NULL UNIQUE,
    correlation_id      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ledger_entries_wallet_created_idx
    ON ledger_entries (wallet_id, created_at DESC, id DESC);

-- Transactional outbox — WalletCreated / WalletCredited are written here
-- in the same DB transaction as the wallet/ledger change, then published
-- to EventBridge by a separate poller (never a direct PutEvents inside a
-- request/consumer transaction).
CREATE TABLE outbox (
    id           BIGSERIAL PRIMARY KEY,
    aggregate_id VARCHAR(64) NOT NULL,   -- wallet id
    event_type   VARCHAR(48) NOT NULL,   -- WalletCreated | WalletCredited
    payload      JSONB NOT NULL,         -- the full EventBridge 'detail'
    published_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
