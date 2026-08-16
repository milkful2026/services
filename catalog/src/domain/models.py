"""Domain models. Plain dataclasses only — no SQLAlchemy/FastAPI types."""

from dataclasses import dataclass
from datetime import date
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
