-- Additive migration (spec MA-107 FR-1). No backfill script needed —
-- DEFAULT satisfies NOT NULL for all existing rows at the column-add
-- itself (Postgres 11+ optimization: a constant DEFAULT on a NOT NULL
-- column addition needs no table rewrite/full scan). No API sets 'B2B'
-- yet; every account is 'B2C' by construction until a B2B onboarding
-- path exists (spec's own deliberate scope limit).

ALTER TABLE users
  ADD COLUMN account_type VARCHAR NOT NULL DEFAULT 'B2C';

-- The CHECK does NOT get the same fast-path as the DEFAULT above —
-- added inline it would scan and lock the whole table under ACCESS
-- EXCLUSIVE for the duration. NOT VALID takes only a brief ACCESS
-- EXCLUSIVE lock to register the constraint; VALIDATE CONSTRAINT then
-- scans under SHARE UPDATE EXCLUSIVE, which still allows concurrent
-- reads/writes on `users` (login, registration) while it runs.
ALTER TABLE users
  ADD CONSTRAINT ck_users_account_type CHECK (account_type IN ('B2C', 'B2B')) NOT VALID;

ALTER TABLE users
  VALIDATE CONSTRAINT ck_users_account_type;
