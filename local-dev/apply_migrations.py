"""Applies each service's raw SQL migration files (the same files that
are production-authoritative for Aurora) against the local Postgres
container. Run after `docker compose up -d`, once the `milkful_user` /
`milkful_inventory` databases exist (created by init-databases.sql on
first container start).

    python apply_migrations.py

Tracks applied filenames in a `schema_migrations` table per database, so
re-running after adding a new migration file only applies the new one.
"""

import os
import sys
from pathlib import Path

import psycopg2

DB_HOST = os.environ.get("LOCAL_DEV_DB_HOST", "localhost")
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"

_SERVICES_DIR = Path(__file__).resolve().parent.parent

_TARGETS = [
    ("user", "milkful_user"),
    ("inventory", "milkful_inventory"),
    ("catalog", "milkful_catalog"),
    ("wallet", "milkful_wallet"),
    ("payment", "milkful_payment"),
    ("identity-auth", "milkful_identity_auth"),
    ("subscription", "milkful_subscription"),
    ("order", "milkful_order"),
]


def _ensure_database_exists(database: str) -> None:
    # init-databases.sql only runs once, on a completely fresh Postgres
    # data volume (docker-entrypoint-initdb.d semantics) — a contributor
    # who already had `milkful-postgres-data` from before a new database
    # was added there would otherwise never get it created, and _apply's
    # own connect() would fail with a bare "database does not exist"
    # OperationalError easily mistaken for "Postgres isn't running".
    # Idempotent: a no-op once the database exists.
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname="postgres"
    )
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction block
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if cur.fetchone() is None:
                print(f"[{database}] database does not exist yet, creating it")
                cur.execute(f'CREATE DATABASE "{database}"')
    finally:
        conn.close()


def _apply(service_dir: str, database: str) -> None:
    migrations_dir = _SERVICES_DIR / service_dir / "migrations"
    if not migrations_dir.is_dir():
        print(f"[{service_dir}] no migrations/ directory, skipping")
        return

    _ensure_database_exists(database)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=database
    )
    try:
        # `with conn:` commits on clean exit and rolls back on exception —
        # psycopg2 connections support this directly, so there's no need
        # to hand-roll autocommit/commit/rollback bookkeeping (and no way
        # to accidentally forget a commit on a future added write).
        with conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(filename VARCHAR(255) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for sql_file in sorted(migrations_dir.glob("*.sql")):
            if sql_file.name in applied:
                print(f"[{service_dir}] {sql_file.name} already applied, skipping")
                continue
            print(f"[{service_dir}] applying {sql_file.name}")
            sql = sql_file.read_text(encoding="utf-8")
            with conn, conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (sql_file.name,)
                )
    finally:
        conn.close()


def main() -> None:
    for service_dir, database in _TARGETS:
        _apply(service_dir, database)
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except psycopg2.OperationalError as exc:
        print(f"Could not connect to Postgres — is `docker compose up -d` running? {exc}", file=sys.stderr)
        sys.exit(1)
