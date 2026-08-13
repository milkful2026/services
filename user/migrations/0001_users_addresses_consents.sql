-- Production-authoritative schema (Postgres/Aurora). IDs are
-- application-generated UUIDs (Python uuid4, stored as text), not
-- DB-side gen_random_uuid() defaults — keeps the SQLAlchemy Core table
-- in src/adapters/user_repository.py portable to the SQLite test double,
-- same reasoning as MA-95's zone_repository.

CREATE TABLE users (
    id                 VARCHAR(36) PRIMARY KEY,
    cognito_sub        VARCHAR(128) NOT NULL UNIQUE,
    name               VARCHAR(100) NOT NULL,
    mobile             VARCHAR(20) NOT NULL,
    email              VARCHAR(255),
    preferred_slot_id  VARCHAR(64),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE addresses (
    id            VARCHAR(36) PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL REFERENCES users(id),
    lines         JSONB NOT NULL,
    city          VARCHAR(100) NOT NULL,
    state         VARCHAR(100) NOT NULL,
    pincode       VARCHAR(6) NOT NULL,
    lat           DOUBLE PRECISION NOT NULL,
    lng           DOUBLE PRECISION NOT NULL,
    landmark      VARCHAR(255),
    is_default    BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_consents (
    id            VARCHAR(36) PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL REFERENCES users(id),
    type          VARCHAR(32) NOT NULL,
    version       VARCHAR(32),
    accepted      BOOLEAN NOT NULL DEFAULT true,
    accepted_at   TIMESTAMPTZ NOT NULL,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Transactional outbox (spec §6). Written in the SAME transaction as the
-- rows above; a separate scheduled Lambda (outbox_publisher_handler)
-- polls WHERE published_at IS NULL and publishes to EventBridge.
CREATE TABLE outbox_events (
    id             VARCHAR(36) PRIMARY KEY,
    aggregate_id   VARCHAR(36) NOT NULL,
    type           VARCHAR(64) NOT NULL,
    payload        JSONB NOT NULL,
    published_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Local delivery-slot reference table (FR-2) — see
-- registration_service.py's module docstring for why this exists
-- instead of a live Inventory call. Sync mechanism from Inventory's
-- actual zone/slot config is NOT wired; this table may simply be empty
-- until a human decides how it gets populated.
CREATE TABLE zone_slots (
    zone_id  VARCHAR(64) NOT NULL,
    slot_id  VARCHAR(64) NOT NULL,
    label    VARCHAR(100) NOT NULL,
    active   BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (zone_id, slot_id)
);

CREATE INDEX idx_outbox_events_unpublished ON outbox_events (created_at) WHERE published_at IS NULL;
