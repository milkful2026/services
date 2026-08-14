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

from psycopg2.extras import Json

from _db import connect, run_guarded
from _zone_seed_data import PINCODE_PREFIXES, SLOTS, ZONE_ID, ZONE_NAME

DB_NAME = "milkful_inventory"


def main() -> None:
    conn = connect(DB_NAME)
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
                    "id": ZONE_ID,
                    "name": ZONE_NAME,
                    "pincode_prefixes": Json(PINCODE_PREFIXES),
                    "slot_config": Json(SLOTS),
                },
            )
        print(f"[inventory] seeded zone {ZONE_ID!r} (pincode prefix 5600, e.g. 560001)")
    finally:
        conn.close()


if __name__ == "__main__":
    run_guarded(main)
