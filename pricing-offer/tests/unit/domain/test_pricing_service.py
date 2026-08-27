import pytest

from domain.exceptions import InvalidRequestError, ProductPricingUnknownError
from domain.models import Frequency, QuoteLineItem
from domain.pricing_service import PricingService


class FakeCatalogClient:
    def __init__(self, prices: dict[str, float] | None = None, error: Exception | None = None):
        self.prices = prices or {}
        self.error = error
        self.requested_product_ids: list[str] = []

    def get_price(self, product_id: str, correlation_id: str = "") -> float:
        self.requested_product_ids.append(product_id)
        if self.error is not None:
            raise self.error
        return self.prices[product_id]


def _service(catalog_client=None, tax_rate_percent=5.0, delivery_fee=20.0) -> PricingService:
    return PricingService(
        catalog_client=catalog_client or FakeCatalogClient({"cow-milk": 68.0}),
        tax_rate_percent=tax_rate_percent,
        delivery_fee=delivery_fee,
    )


def test_one_time_quote_computes_tax_and_delivery_fee():
    service = _service()
    items = [QuoteLineItem(product_id="cow-milk", quantity=2, frequency=Frequency.ONE_TIME)]

    result = service.quote(items, delivery_state="Karnataka")

    # base = 68 * 2 = 136; tax = 136 * 5% = 6.8; net = 136 + 6.8 + 20 = 162.8
    assert result.base_price == 136.0
    assert result.tax_amount == 6.8
    assert result.tax_rate == 5.0
    assert result.delivery_fee == 20.0
    assert result.net_payable == 162.8
    assert result.monthly_estimate is None


def test_daily_subscription_quote_includes_a_tax_and_delivery_inclusive_monthly_estimate():
    service = _service()
    items = [QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.DAILY)]

    result = service.quote(items, delivery_state="Karnataka")

    # net_payable per delivery = 68 + 3.4 + 20 = 91.4; monthly = 91.4 * 30
    assert result.net_payable == 91.4
    assert result.monthly_estimate == 2742.0


def test_alternate_days_uses_15_occurrences_per_month():
    service = _service()
    items = [QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.ALTERNATE_DAYS)]

    result = service.quote(items, delivery_state="Karnataka")

    assert result.monthly_estimate == round(result.net_payable * 15, 2)


def test_multiple_line_items_sum_into_one_cart_level_quote():
    catalog_client = FakeCatalogClient({"cow-milk": 68.0, "cow-ghee": 650.0})
    service = _service(catalog_client=catalog_client)
    items = [
        QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME),
        QuoteLineItem(product_id="cow-ghee", quantity=1, frequency=Frequency.ONE_TIME),
    ]

    result = service.quote(items, delivery_state="Karnataka")

    assert result.base_price == 718.0
    # Items are fetched concurrently now, so only the *set* of requested
    # products (not the order) is guaranteed.
    assert sorted(catalog_client.requested_product_ids) == ["cow-ghee", "cow-milk"]


def test_mixed_frequencies_across_line_items_omit_the_monthly_estimate():
    catalog_client = FakeCatalogClient({"cow-milk": 68.0, "cow-ghee": 650.0})
    service = _service(catalog_client=catalog_client)
    items = [
        QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.DAILY),
        QuoteLineItem(product_id="cow-ghee", quantity=1, frequency=Frequency.ONE_TIME),
    ]

    result = service.quote(items, delivery_state="Karnataka")

    assert result.monthly_estimate is None


def test_empty_items_is_rejected():
    service = _service()

    with pytest.raises(InvalidRequestError):
        service.quote([], delivery_state="Karnataka")


def test_missing_delivery_state_is_rejected():
    service = _service()
    items = [QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME)]

    with pytest.raises(InvalidRequestError):
        service.quote(items, delivery_state=None)


def test_non_positive_quantity_is_rejected():
    service = _service()
    items = [QuoteLineItem(product_id="cow-milk", quantity=0, frequency=Frequency.ONE_TIME)]

    with pytest.raises(InvalidRequestError):
        service.quote(items, delivery_state="Karnataka")


def test_unknown_product_propagates_product_pricing_unknown():
    catalog_client = FakeCatalogClient(error=ProductPricingUnknownError("no such product"))
    service = _service(catalog_client=catalog_client)
    items = [QuoteLineItem(product_id="ghost", quantity=1, frequency=Frequency.ONE_TIME)]

    with pytest.raises(ProductPricingUnknownError):
        service.quote(items, delivery_state="Karnataka")
