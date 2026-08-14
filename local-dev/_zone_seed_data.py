"""Shared local zone/slot fixture data, so `seed_inventory_zones.py` and
`seed_user_zone_slots.py` can't drift out of sync with each other — see
user/README.md's flagged decision #1: these two tables aren't synced by
any mechanism outside local-dev.
"""

ZONE_ID = "blr-central"
ZONE_NAME = "Bangalore Central"
PINCODE_PREFIXES = ["5600"]

SLOTS = [
    {"id": "morning-6-8", "label": "Morning 6-8 AM"},
    {"id": "evening-6-8", "label": "Evening 6-8 PM"},
]
