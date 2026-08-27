"""POST /pricing/quote — MA-101/MA-122 FR-1."""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from domain.exceptions import InvalidRequestError
from domain.models import Frequency, QuoteLineItem
from domain.pricing_service import PricingService
from handlers.dependencies import get_pricing_service
from handlers.dto import serialize_quote, success_envelope

router = APIRouter(tags=["pricing"])


class QuoteLineItemDto(BaseModel):
    product_id: str = Field(alias="productId", min_length=1)
    quantity: int
    frequency: str

    model_config = {"populate_by_name": True}


class QuoteRequestDto(BaseModel):
    items: list[QuoteLineItemDto]
    delivery_state: str | None = Field(alias="deliveryState", default=None)
    # Accepted for contract parity with MA-122 FR-1 but never applied —
    # no Offers system exists in this build (see README's "Scope"
    # section). The mobile client (this service's only real caller today)
    # never sends a value here either way.
    offer_code: str | None = Field(alias="offerCode", default=None)

    model_config = {"populate_by_name": True}


@router.post("/pricing/quote")
def quote(
    request: QuoteRequestDto,
    http_request: Request,
    service: PricingService = Depends(get_pricing_service),
):
    # Threaded through to the catalog client as a request parameter (never
    # stored on the singleton service/client instances) so concurrent
    # requests can't leak each other's correlation IDs into their logs.
    correlation_id = http_request.headers.get("x-request-id", str(uuid.uuid4()))
    items = [
        QuoteLineItem(
            product_id=item.product_id,
            quantity=item.quantity,
            frequency=_parse_frequency(item.frequency),
        )
        for item in request.items
    ]
    result = service.quote(items, request.delivery_state, correlation_id)
    return success_envelope(serialize_quote(result))


def _parse_frequency(value: str) -> Frequency:
    try:
        return Frequency(value)
    except ValueError as exc:
        raise InvalidRequestError(f"Unrecognized frequency: {value}") from exc
