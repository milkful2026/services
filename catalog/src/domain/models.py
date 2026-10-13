"""Domain models. Plain dataclasses only — no SQLAlchemy/FastAPI types."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class StockState(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    AVAILABLE_FROM = "AVAILABLE_FROM"


@dataclass
class Category:
    id: str
    name: str
    icon_name: str
    sort_order: int = 0


@dataclass
class Product:
    id: str
    category_id: str
    name: str
    description: str
    unit: str
    price_b2c: float
    price_b2b: float | None
    image_url: str | None
    tag: str | None
    subscription_eligible: bool
    is_veg: bool
    is_organic: bool
    stock_state: StockState
    available_from: date | None
    # MA-156 (MA-42) — admin-write fields. Defaulted so every existing
    # call site that constructs a Product positionally/by-keyword without
    # them (tests, StockChanged application) keeps working unchanged.
    is_active: bool = True
    hsn_code: str | None = None
    tax_rate: float | None = None


@dataclass
class SearchFilters:
    category_ids: list[str]
    min_price: float | None = None
    max_price: float | None = None
    veg_only: bool = False
    organic_only: bool = False


class SortOrder(StrEnum):
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    NEWEST = "newest"


# --- MA-156 (MA-42): admin write API --------------------------------------


@dataclass
class CatalogAuditLogEntry:
    """MA-156 §7 — a generic action/previous_value/new_value JSONB diff,
    not a copy of Inventory's typed-integer `inventory_audit_log`: a
    product create/edit can change any of a dozen heterogeneous fields at
    once (§11 Risk 5)."""

    id: str
    product_id: str
    admin_id: str
    action: str  # "CREATE" | "UPDATE"
    previous_value: dict | None
    new_value: dict | None
    reason: str | None
    created_at: datetime | None = None


@dataclass
class AdminProductSummary:
    """FR-4's admin-list row shape — a distinct, narrower projection from
    `Product` (adds created_at/updated_at, which the public read paths
    never need), same reasoning as Inventory's `StockSummary` being kept
    separate from its own `Stock`."""

    id: str
    category_id: str
    name: str
    price_b2c: float
    price_b2b: float | None
    is_active: bool
    stock_state: StockState
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class Page:
    """Generic pagination envelope — shared by `GET /admin/products` and
    `GET /products/{id}/audit-log` (MA-156 FR-4/FR-5), same shape as
    Inventory's own `Page` (MA-150 FR-4)."""

    items: list
    total: int
    page: int
    page_size: int
