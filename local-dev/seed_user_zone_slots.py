"""Seeds User Service's own `zone_slots` table so `GET /delivery/slots?
zoneId=` (called by the Flutter app's slot-selection screen, per MA-1's
spec FR-7) returns real slots locally instead of an empty list.

user/README.md's own flagged decision #1 already documents that this
table isn't wired to Inventory's real zone config in any environment —
`seed_inventory_zones.py` seeds Inventory's separate `serviceability_zones`
table with the *same* zone id (`blr-central`) and slot ids used here, so
the two independently-seeded tables agree locally, but this is a
pragmatic local-dev fix, not a change to that still-unresolved
production sync question.

Run after apply_migrations.py (and ideally alongside seed_inventory_zones.py):

    python seed_user_zone_slots.py
"""

import psycopg2

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "milkful"
DB_PASSWORD = "milkful"
DB_NAME = "milkful_user"

_ZONE_ID = "blr-central"
_SLOTS = [
    {"slot_id": "morning-6-8", "label": "Morning 6-8 AM"},
    {"slot_id": "evening-6-8", "label": "Evening 6-8 PM"},
]


def main() -> None:
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    )
    try:
        with conn, conn.cursor() as cur:
            for slot in _SLOTS:
                cur.execute(
                    """
                    INSERT INTO zone_slots (zone_id, slot_id, label, active)
                    VALUES (%(zone_id)s, %(slot_id)s, %(label)s, true)
                    ON CONFLICT (zone_id, slot_id) DO UPDATE SET
                        label = EXCLUDED.label,
                        active = true
                    """,
                    {"zone_id": _ZONE_ID, "slot_id": slot["slot_id"], "label": slot["label"]},
                )
        print(f"[user] seeded {len(_SLOTS)} slot(s) for zone {_ZONE_ID!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
