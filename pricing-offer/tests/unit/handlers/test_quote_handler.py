import pytest
from fastapi.testclient import TestClient

from domain.exceptions import (
    InvalidRequestError,
    ProductPricingUnknownError,
    ServiceUnavailableError,
)
from domain.models import Frequency, Quote, QuoteLineItem
from handlers.app import app
from handlers.dependencies import get_pricing_service


class FakePricingService:
    def __init__(self, quote_result: Quote | None = None, error: Exception | None = None):
        self.quote_result = quote_result
        self.error = error
        self.quote_calls: list[tuple] = []

    def quote(self, items: list[QuoteLineItem], delivery_state, correlation_id: str = ""):
        self.quote_calls.append((items, delivery_state, correlation_id))
        if self.error is not None:
            raise self.error
        return self.quote_result


_QUOTE = Quote(
    base_price=68.0,
    tax_amount=3.4,
    tax_rate=5.0,
    delivery_fee=20.0,
    net_payable=91.4,
    monthly_estimate=None,
)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _override(fake_service: FakePricingService) -> FakePricingService:
    app.dependency_overrides[get_pricing_service] = lambda: fake_service
    return fake_service


@pytest.fixture
def client():
    return TestClient(app)


def test_quote_success_returns_the_serialized_quote(client):
    service = _override(FakePricingService(quote_result=_QUOTE))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"] == {
        "basePrice": 68.0,
        "taxAmount": 3.4,
        "taxRate": 5.0,
        "deliveryFee": 20.0,
        "netPayable": 91.4,
        "monthlyEstimate": None,
        "discountAmount": None,
        "appliedOfferId": None,
    }
    items, delivery_state, correlation_id = service.quote_calls[0]
    assert items == [
        QuoteLineItem(product_id="cow-milk", quantity=1, frequency=Frequency.ONE_TIME)
    ]
    assert delivery_state == "Karnataka"
    assert correlation_id  # generated when the client sends no x-request-id


def test_quote_offer_code_is_accepted_but_not_required(client):
    _override(FakePricingService(quote_result=_QUOTE))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
            "offerCode": "WELCOME10",
        },
    )

    assert response.status_code == 200


def test_quote_unrecognized_frequency_maps_to_400(client):
    _override(FakePricingService())

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "WEEKLY"}],
            "deliveryState": "Karnataka",
        },
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "INVALID_REQUEST"


def test_quote_missing_items_is_a_422(client):
    _override(FakePricingService())

    response = client.post("/pricing/quote", json={"deliveryState": "Karnataka"})

    assert response.status_code == 422


def test_quote_blank_product_id_is_a_422_not_a_catalog_call(client):
    service = _override(FakePricingService(quote_result=_QUOTE))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
        },
    )

    assert response.status_code == 422
    assert service.quote_calls == []


def test_quote_propagates_the_inbound_x_request_id_as_correlation_id(client):
    service = _override(FakePricingService(quote_result=_QUOTE))

    client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
        },
        headers={"x-request-id": "caller-corr-id"},
    )

    _, _, correlation_id = service.quote_calls[0]
    assert correlation_id == "caller-corr-id"


def test_quote_invalid_request_from_domain_maps_to_400(client):
    _override(FakePricingService(error=InvalidRequestError("deliveryState is required")))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": None,
        },
    )

    assert response.status_code == 400
    assert response.json()["data"]["errorCode"] == "INVALID_REQUEST"


def test_quote_product_pricing_unknown_maps_to_404(client):
    _override(FakePricingService(error=ProductPricingUnknownError("no such product")))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "ghost", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
        },
    )

    assert response.status_code == 404
    assert response.json()["data"]["errorCode"] == "PRODUCT_PRICING_UNKNOWN"


def test_quote_service_unavailable_maps_to_503(client):
    _override(FakePricingService(error=ServiceUnavailableError("catalog down")))

    response = client.post(
        "/pricing/quote",
        json={
            "items": [{"productId": "cow-milk", "quantity": 1, "frequency": "ONE_TIME"}],
            "deliveryState": "Karnataka",
        },
    )

    assert response.status_code == 503
    assert response.json()["data"]["errorCode"] == "SERVICE_UNAVAILABLE"


def test_healthz_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
