"""Seeds User Service's own `zone_slots` table so `GET /delivery/slots?
zoneId=` (called by the Flutter app's slot-selection screen, per MA-1's
spec FR-7) returns real slots locally instead of an empty list.

user/README.md's own flagged decision #1 already documents that this
table isn't wired to Inventory's real zone config in any environment —
`seed_inventory_zones.py` seeds Inventory's separate `serviceability_zones`
table with the *same* zone id and slot ids (shared via `_zone_seed_data.py`
so the two can't silently drift), so the two independently-seeded tables
agree locally, but this is a pragmatic local-dev fix, not a change to that
still-unresolved production sync question.

Run after apply_migrations.py (and ideally alongside seed_inventory_zones.py):

    python seed_user_zone_slots.py
"""

from _db import connect, run_guarded
from _zone_seed_data import SLOTS, ZONE_ID

DB_NAME = "milkful_user"


def main() -> None:
    conn = connect(DB_NAME)
    try:
        with conn, conn.cursor() as cur:
            for slot in SLOTS:
                cur.execute(
                    """
                    INSERT INTO zone_slots (zone_id, slot_id, label, active)
                    VALUES (%(zone_id)s, %(slot_id)s, %(label)s, true)
                    ON CONFLICT (zone_id, slot_id) DO UPDATE SET
                        label = EXCLUDED.label,
                        active = true
                    """,
                    {"zone_id": ZONE_ID, "slot_id": slot["id"], "label": slot["label"]},
                )
        print(f"[user] seeded {len(SLOTS)} slot(s) for zone {ZONE_ID!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    run_guarded(main)
