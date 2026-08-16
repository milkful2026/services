"""Seeds demo categories/products into local Postgres so the Flutter
catalog screen (MA-22/MA-115) has something real to render — there's no
admin/write API for the catalog yet (MA-116 explicitly deferred it to
MA-42), so direct SQL is the only local option, same reasoning as
seed_inventory_zones.py.

Run after apply_migrations.py:

    python seed_catalog_products.py

Safe to re-run — upserts on id.
"""

from _catalog_seed_data import CATEGORIES, PRODUCTS
from _db import connect, run_guarded

DB_NAME = "milkful_catalog"


def main() -> None:
    conn = connect(DB_NAME)
    try:
        with conn, conn.cursor() as cur:
            for category in CATEGORIES:
                cur.execute(
                    """
                    INSERT INTO categories (id, name, icon_name, sort_order)
                    VALUES (%(id)s, %(name)s, %(icon_name)s, %(sort_order)s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        icon_name = EXCLUDED.icon_name,
                        sort_order = EXCLUDED.sort_order
                    """,
                    category,
                )
            for product in PRODUCTS:
                cur.execute(
                    """
                    INSERT INTO products (
                        id, category_id, name, description, unit, price_b2c,
                        tag, subscription_eligible, is_veg, is_organic
                    )
                    VALUES (
                        %(id)s, %(category_id)s, %(name)s, %(description)s, %(unit)s,
                        %(price_b2c)s, %(tag)s, %(subscription_eligible)s, %(is_veg)s,
                        %(is_organic)s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        category_id = EXCLUDED.category_id,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        unit = EXCLUDED.unit,
                        price_b2c = EXCLUDED.price_b2c,
                        tag = EXCLUDED.tag,
                        subscription_eligible = EXCLUDED.subscription_eligible,
                        is_veg = EXCLUDED.is_veg,
                        is_organic = EXCLUDED.is_organic
                    """,
                    product,
                )
        print(f"[catalog] seeded {len(CATEGORIES)} categories, {len(PRODUCTS)} products")
    finally:
        conn.close()


if __name__ == "__main__":
    run_guarded(main)
