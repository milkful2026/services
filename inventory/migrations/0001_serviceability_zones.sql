-- Production-authoritative schema (Postgres/Aurora). The SQLAlchemy Core
-- table in src/adapters/zone_repository.py must stay column-for-column
-- compatible with this — pincode_prefixes and polygon are JSONB here
-- (not native Postgres arrays / PostGIS geometry) specifically so the same
-- Core table definition also works against the SQLite test double.
--
-- point-in-polygon matching happens in Python (shapely), not SQL — this
-- repository only ever does `WHERE active = true` and hands the raw rows
-- to the domain layer, so no PostGIS extension or prefix index is needed
-- here despite the spec's §7 mention of one; the "index" is effectively
-- in-memory Python matching over a small, cacheable zone list.

CREATE TABLE serviceability_zones (
    id                 VARCHAR(64) PRIMARY KEY,
    name               VARCHAR(255) NOT NULL,
    active             BOOLEAN NOT NULL DEFAULT true,
    pincode_prefixes   JSONB NOT NULL DEFAULT '[]',   -- array of prefix strings, e.g. ["5600", "5601"]
    polygon            JSONB,                          -- array of [lat, lng] pairs (closed ring); NULL if unset
    slot_config        JSONB NOT NULL DEFAULT '[]',    -- array of {"id": "...", "label": "..."}
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_serviceability_zones_active ON serviceability_zones (active) WHERE active = true;
