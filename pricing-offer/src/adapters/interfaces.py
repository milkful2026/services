"""Abstract adapter interfaces (Protocols). Domain code depends on these
only, never on `requests` directly — matches catalog's own
adapters/interfaces.py convention."""

from typing import Protocol


class CatalogClientPort(Protocol):
    def get_price(self, product_id: str) -> float:
        """Returns the product's current B2C price. Raises
        [ProductPricingUnknownError][domain.exceptions.ProductPricingUnknownError]
        if Catalog Service has no such product, or
        [ServiceUnavailableError][domain.exceptions.ServiceUnavailableError]
        if Catalog Service is unreachable after retries."""
        ...
