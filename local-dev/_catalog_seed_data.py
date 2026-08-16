"""Shared category/product fixture data for seed_catalog_products.py.
Category ids/icon names match `_iconFor` in
milkful-app/lib/features/catalog/presentation/catalog_screen.dart exactly,
so a fresh local run renders real icons instead of falling back to the
generic storefront one. Products mirror MA-22's reference mockup (Cow
Milk / Buffalo Milk / Low Fat Milk, same prices/units/tags) as a
recognizable default, not because the mockup's exact catalog is a real
requirement — MA-22 §11 Risk #3 already documents that most of this
app's catalog UI states/content were designed without a content source
of truth.
"""

CATEGORIES = [
    {"id": "milk", "name": "Fresh Milk", "icon_name": "milk", "sort_order": 0},
    {"id": "curd", "name": "Yogurt & Curd", "icon_name": "curd", "sort_order": 1},
    {"id": "paneer", "name": "Paneer", "icon_name": "paneer", "sort_order": 2},
    {"id": "ghee", "name": "Pure Ghee", "icon_name": "ghee", "sort_order": 3},
    {"id": "veggies", "name": "Veggies", "icon_name": "veggies", "sort_order": 4},
]

PRODUCTS = [
    {
        "id": "cow-milk",
        "category_id": "milk",
        "name": "Cow Milk",
        "description": "Farm-fresh cow milk, pasteurized daily.",
        "unit": "1L Bottle",
        "price_b2c": 68,
        "tag": "ORGANIC",
        "subscription_eligible": True,
        "is_veg": True,
        "is_organic": True,
    },
    {
        "id": "buffalo-milk",
        "category_id": "milk",
        "name": "Buffalo Milk",
        "description": "Rich, creamy buffalo milk.",
        "unit": "1L Pouch",
        "price_b2c": 84,
        "tag": "RICH",
        "subscription_eligible": True,
        "is_veg": True,
        "is_organic": False,
    },
    {
        "id": "low-fat-milk",
        "category_id": "milk",
        "name": "Low Fat Milk",
        "description": "Toned milk, lower fat content.",
        "unit": "500ml Pouch",
        "price_b2c": 32,
        "tag": "HEALTHY",
        "subscription_eligible": False,
        "is_veg": True,
        "is_organic": False,
    },
    {
        "id": "set-curd",
        "category_id": "curd",
        "name": "Fresh Curd",
        "description": "Thick, set curd made from full-cream milk.",
        "unit": "400g Cup",
        "price_b2c": 45,
        "tag": None,
        "subscription_eligible": True,
        "is_veg": True,
        "is_organic": False,
    },
    {
        "id": "greek-yogurt",
        "category_id": "curd",
        "name": "Greek Yogurt",
        "description": "Strained, high-protein yogurt.",
        "unit": "200g Cup",
        "price_b2c": 60,
        "tag": "PROTEIN",
        "subscription_eligible": False,
        "is_veg": True,
        "is_organic": False,
    },
    {
        "id": "malai-paneer",
        "category_id": "paneer",
        "name": "Malai Paneer",
        "description": "Soft, malai-style paneer.",
        "unit": "200g Pack",
        "price_b2c": 90,
        "tag": None,
        "subscription_eligible": False,
        "is_veg": True,
        "is_organic": False,
    },
    {
        "id": "cow-ghee",
        "category_id": "ghee",
        "name": "Cow Ghee",
        "description": "Traditional bilona-method cow ghee.",
        "unit": "500ml Jar",
        "price_b2c": 450,
        "tag": "ORGANIC",
        "subscription_eligible": False,
        "is_veg": True,
        "is_organic": True,
    },
    {
        "id": "spinach",
        "category_id": "veggies",
        "name": "Spinach",
        "description": "Fresh, locally grown spinach.",
        "unit": "250g Bunch",
        "price_b2c": 25,
        "tag": None,
        "subscription_eligible": False,
        "is_veg": True,
        "is_organic": True,
    },
]
