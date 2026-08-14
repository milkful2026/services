"""Seeds one serviceability zone into local Postgres so a full local
registration run-through actually gets a "serviceable" response from
Inventory, instead of a 404/empty result (there's no admin/write API for
zones — MA-95 is read-only — so direct SQL is the only local option).

Run after apply_migrations.py:

    python seed_inventory_zones.py

Matches the pincode (560001) used throughout the other services' own
test fixtures, so the default curl walkthrough in README.md works
without editing anything. Safe to re-run — upserts on the zone id.
"""

import psycopg2
from psycopg2.extras import Json

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"
DB_NAME = "milkful_inventory"

_ZONE = {
    "id": "blr-central",
    "name": "Bangalore Central",
    "pincode_prefixes": ["5600"],
    "slot_config": [
        {"id": "morning-6-8", "label": "Morning 6-8 AM"},
        {"id": "evening-6-8", "label": "Evening 6-8 PM"},
    ],
}


def main() -> None:
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO serviceability_zones (id, name, active, pincode_prefixes, slot_config)
                VALUES (%(id)s, %(name)s, true, %(pincode_prefixes)s, %(slot_config)s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    active = true,
                    pincode_prefixes = EXCLUDED.pincode_prefixes,
                    slot_config = EXCLUDED.slot_config
                """,
                {
                    "id": _ZONE["id"],
                    "name": _ZONE["name"],
                    "pincode_prefixes": Json(_ZONE["pincode_prefixes"]),
                    "slot_config": Json(_ZONE["slot_config"]),
                },
            )
        print(f"[inventory] seeded zone {_ZONE['id']!r} (pincode prefix 5600, e.g. 560001)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
