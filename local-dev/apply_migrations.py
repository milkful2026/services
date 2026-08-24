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
]


def _apply(service_dir: str, database: str) -> None:
    migrations_dir = _SERVICES_DIR / service_dir / "migrations"
    if not migrations_dir.is_dir():
        print(f"[{service_dir}] no migrations/ directory, skipping")
        return

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
