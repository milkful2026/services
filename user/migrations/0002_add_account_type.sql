-- Additive migration (spec MA-107 FR-1). No backfill script needed —
-- DEFAULT satisfies NOT NULL for all existing rows at the column-add
-- itself. No API sets 'B2B' yet; every account is 'B2C' by construction
-- until a B2B onboarding path exists (spec's own deliberate scope limit).

ALTER TABLE users
  ADD COLUMN account_type VARCHAR NOT NULL DEFAULT 'B2C'
  CHECK (account_type IN ('B2C', 'B2B'));
