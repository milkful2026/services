-- Additive migration (MA-25 Step 6 backend companion — review-found gap).
-- `zone_id` was already resolved client-side during address entry (the
-- mobile app's own checkServiceability call) but never persisted; this
-- column gives GET /users/me a real defaultAddressZoneId for returning
-- users, mirroring the precedent MA-23 set for `state` -> defaultAddressState.
-- Nullable — every existing address row, and any registration made before
-- the mobile client starts sending zoneId, has none.

ALTER TABLE addresses
  ADD COLUMN zone_id VARCHAR(64);
