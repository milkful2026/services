"""Shared local-Postgres connection config for local-dev scripts, so a
credential/port change is a one-file edit instead of a per-script one.
"""

import sys

import psycopg2

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"


def connect(dbname: str):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=dbname
    )


def run_guarded(main) -> None:
    """Runs `main`, printing a friendly message instead of a raw traceback
    when Postgres isn't reachable or a table doesn't exist yet."""
    try:
        main()
    except psycopg2.OperationalError as exc:
        print(f"Could not connect to Postgres — is `docker compose up -d` running? {exc}", file=sys.stderr)
        sys.exit(1)
    except psycopg2.errors.UndefinedTable as exc:
        print(f"Table not found — have you run `python apply_migrations.py` yet? {exc}", file=sys.stderr)
        sys.exit(1)
