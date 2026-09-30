-- MA-139 code-review fix: tracks whether a users row's DB status and its
-- Cognito enabled/disabled state may have drifted because a prior
-- AdminDisableUser/AdminEnableUser call failed after the status-change DB
-- transaction already committed (CognitoSyncFailedError, spec section 6/11
-- Risk 1). Additive migration, same fast-path shape as
-- 0004_customer_status.sql (plain NOT NULL DEFAULT needs no table rewrite).

ALTER TABLE users
  ADD COLUMN cognito_sync_pending BOOLEAN NOT NULL DEFAULT false;

-- Partial index: only the (expected to be rare) rows a prior Cognito call
-- actually failed for are ever queried, by the FR-7 sweep's
-- drift-reconciliation pass.
CREATE INDEX idx_users_cognito_sync_pending
  ON users (id)
  WHERE cognito_sync_pending = true;
