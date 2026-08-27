"""MA-101/MA-122 FR-1's `POST /pricing/quote` — the actual business logic
`quote_handler.py` calls, kept independent of FastAPI/pydantic entirely.

**Deliberately scoped down from MA-122's full merged spec** — see this
service's README "Scope" section for the complete list and reasoning.
The two simplifications that shape this file specifically:

- A single flat [Settings.default_tax_rate_percent][config.env.Settings]
  applies to every line item, not a real per-product HSN/GST rate (no
  `CatalogUpdated` event pipeline or Catalog Service schema change exists
  in this build to source one from).
- No CGST/SGST-vs-IGST split — the mobile client's `Quote` model
  (`lib/features/cart/models/quote.dart`) only has one `taxAmount`/
  `taxRate` pair; the split only affects how the *same* total tax is
  divided for GST reporting, not the total itself, so `deliveryState` is
  validated as present (per MA-122 FR-1's contract) but not otherwise
  used here.
"""

from concurrent.futures import ThreadPoolExecutor

from adapters.interfaces import CatalogClientPort
from domain.exceptions import InvalidRequestError
from domain.models import Frequency, Quote, QuoteLineItem


class PricingService:
    def __init__(
        self,
        catalog_client: CatalogClientPort,
        tax_rate_percent: float,
        delivery_fee: float,
    ) -> None:
        self._catalog_client = catalog_client
        self._tax_rate_percent = tax_rate_percent
        self._delivery_fee = delivery_fee

    def quote(
        self,
        items: list[QuoteLineItem],
        delivery_state: str | None,
        correlation_id: str = "",
    ) -> Quote:
        if not items:
            raise InvalidRequestError("items must not be empty")
        if not delivery_state:
            # MA-122 FR-1: required for the real spec's CGST/SGST-vs-IGST
            # determination — this build doesn't use the value (see module
            # docstring) but still enforces its presence, so a caller
            # relying on the documented contract fails the same way it
            # would against the full implementation, not silently.
            raise InvalidRequestError("deliveryState is required")
        for item in items:
            if item.quantity < 1:
                raise InvalidRequestError(f"quantity must be at least 1 (got {item.quantity})")

        # Fan out to Catalog concurrently — each item's HTTP call (with its
        # own retry/backoff) is independent, so N items shouldn't cost N×
        # a single item's latency. ThreadPoolExecutor.map preserves result
        # order to match `items`, regardless of completion order.
        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            prices = list(
                executor.map(
                    lambda item: self._catalog_client.get_price(item.product_id, correlation_id),
                    items,
                )
            )
        base_price = sum(
            price * item.quantity for price, item in zip(prices, items, strict=True)
        )
        tax_amount = round(base_price * self._tax_rate_percent / 100, 2)
        net_payable = round(base_price + tax_amount + self._delivery_fee, 2)

        return Quote(
            base_price=round(base_price, 2),
            tax_amount=tax_amount,
            tax_rate=self._tax_rate_percent,
            delivery_fee=self._delivery_fee,
            net_payable=net_payable,
            monthly_estimate=self._monthly_estimate(items, net_payable),
        )

    def _monthly_estimate(self, items: list[QuoteLineItem], net_payable: float) -> float | None:
        # MA-122 FR-1's own PR #9 fix: the monthly estimate is
        # tax/delivery-inclusive — `net_payable × occurrences`, never unit
        # price alone. Only meaningful when every line item shares one
        # subscription frequency (see Quote's own docstring for why a
        # mixed-frequency request leaves this None rather than guessing).
        frequencies = {item.frequency for item in items}
        if len(frequencies) != 1:
            return None
        (frequency,) = frequencies
        if frequency == Frequency.ONE_TIME:
            return None
        return round(net_payable * frequency.monthly_occurrences, 2)
