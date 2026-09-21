-- Admin Identity, RBAC & Session Security (MA-129 §7).
-- Same conventions as services/user's migrations: application-generated
-- UUIDs (Python uuid4, stored as text), not DB-side gen_random_uuid()
-- defaults, so the SQLAlchemy Core table in
-- src/adapters/admin_user_repository.py stays portable to the SQLite
-- test double used offline (documented fidelity gap, same as `user`).
--
-- Owned exclusively by Identity & Auth in a NEW Aurora instance for this
-- service — never the `user` service's database (database-per-service,
-- services/README.md §1/§3.6).

CREATE TABLE admin_user (
    id                        VARCHAR(36) PRIMARY KEY,
    cognito_sub               VARCHAR(128) NOT NULL UNIQUE,
    name                      VARCHAR(100) NOT NULL,
    email                     VARCHAR(255) NOT NULL UNIQUE,
    role                      VARCHAR(16) NOT NULL,
    status                    VARCHAR(16) NOT NULL,
    ip_allowlist              JSONB,
    max_concurrent_sessions   INTEGER,
    last_login_at             TIMESTAMPTZ,
    created_by                VARCHAR(36) REFERENCES admin_user(id),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_admin_user_role CHECK (role IN ('Ops', 'Finance', 'Support', 'Marketing', 'SuperAdmin')),
    CONSTRAINT ck_admin_user_status CHECK (status IN ('Pending', 'Active', 'Deactivated'))
);

CREATE INDEX idx_admin_user_role ON admin_user (role);
CREATE INDEX idx_admin_user_status ON admin_user (status);

-- Supports FR-4's list endpoint name/email search (spec: "name/email
-- search"). A simple case-insensitive LIKE index is sufficient at the
-- "low hundreds of accounts" scale the NFR anticipates (§5 Scalability)
-- — no full-text/trigram index needed.
CREATE INDEX idx_admin_user_email_lower ON admin_user (lower(email));
CREATE INDEX idx_admin_user_name_lower ON admin_user (lower(name));

-- **Migration note (spec §7):** the very first Super-Admin account has
-- no created_by and cannot be created via POST /v1/admin/users (that
-- endpoint is itself Super-Admin-only — no Super-Admin exists yet to
-- call it). Seeding it is NOT part of this migration — see
-- scripts/bootstrap_super_admin.py, which a human runs once, out of
-- band, against a real Aurora + Admin Cognito Pool, per
-- services/README.md §3.6 ("production migrations require human
-- approval"). This resolves spec Open Question §12.3.
