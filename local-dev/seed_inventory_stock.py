"""Provisions a `stock` row (on_hand=0, reserved=0) for every seeded
catalog product (see _catalog_seed_data.py), by publishing a real
`CatalogUpdated`-shaped event through this stack's own EventBridge/SQS
wiring (bootstrap.py's `CatalogUpdatedRule` -> `catalog-updated` queue) —
not a direct SQL insert like seed_inventory_zones.py/
seed_catalog_products.py use for their own tables.

This is deliberately NOT a direct-SQL seed, for two reasons:
1. It is this story's own, best-available way to exercise
   adapters/catalog_updated_consumer.py (MA-118 FR-8) against a *real*
   EventBridge delivery rather than only a direct-to-SQS unit test — see
   that consumer's module docstring for why Catalog itself cannot do
   this for real yet (no outbox/publish mechanism exists there at all).
2. It only provisions the empty `stock` row FR-8 defines — it does NOT
   give any product real quantity. Run `seed_inventory_batches.py`
   (or call `POST /inventory/receive` directly) afterward to actually
   stock a product, matching MA-118 §12 Q3's own documented split
   between "provisioning" and "goods receipt".

Run after `docker compose up -d` and `python bootstrap.py` (needs the
CatalogUpdatedRule/queue bootstrap.py just wired, and the `inventory`
container's CatalogUpdatedConsumer thread running to actually drain the
queue — give it a few seconds):

    python seed_inventory_stock.py
"""

import json
import os

import boto3

from _catalog_seed_data import PRODUCTS

ENDPOINT_URL = os.environ.get("LOCAL_DEV_AWS_ENDPOINT_URL", "http://localhost:5000")
REGION = "us-east-1"

_creds = dict(
    aws_access_key_id="local",
    aws_secret_access_key="local",
    region_name=REGION,
    endpoint_url=ENDPOINT_URL,
)


def main() -> None:
    events = boto3.client("events", **_creds)

    entries = [
        {
            "Source": "catalog",
            "DetailType": "CatalogUpdated",
            "Detail": json.dumps(
                {"payload": {"productId": product["id"]}, "correlationId": f"seed-{product['id']}"}
            ),
            "EventBusName": "default",
        }
        for product in PRODUCTS
    ]
    # put_events accepts at most 10 entries per call.
    for i in range(0, len(entries), 10):
        events.put_events(Entries=entries[i : i + 10])

    print(
        f"[inventory] published CatalogUpdated for {len(entries)} product(s) — "
        "give the inventory container's catalog-updated-consumer thread a few "
        "seconds, then confirm with: curl http://localhost:8000/v1/inventory/cow-milk"
    )


if __name__ == "__main__":
    main()
