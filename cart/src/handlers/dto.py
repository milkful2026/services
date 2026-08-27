"""Request/response DTOs and response-envelope helpers. Fixed envelope
shape per services/README.md §5 — identical to every other service's
`{requestId, status, data}` shape.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from domain.exceptions import CartServiceError, ValidationError
from domain.models import Cart, CartView, Frequency, LineItem, Quote


def extract_jwt_claims(event: dict) -> dict:
    """Verified claims from the API Gateway HTTP API JWT authorizer —
    same convention as every other public route in this codebase."""
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


class AddItemRequestDto(BaseModel):
    product_id: str = Field(alias="productId", min_length=1)
    quantity: int
    frequency: str
    start_date: str | None = Field(alias="startDate", default=None)

    model_config = {"populate_by_name": True}

    @field_validator("frequency")
    @classmethod
    def _validate_frequency(cls, value: str) -> str:
        try:
            Frequency(value)
        except ValueError as exc:
            raise ValueError(f"Unrecognized frequency: {value}") from exc
        return value

    def to_frequency(self) -> Frequency:
        return Frequency(self.frequency)


class ReplaceCartItemDto(BaseModel):
    id: str | None = None
    product_id: str = Field(alias="productId", min_length=1)
    quantity: int
    frequency: str
    start_date: str | None = Field(alias="startDate", default=None)

    model_config = {"populate_by_name": True}

    @field_validator("frequency")
    @classmethod
    def _validate_frequency(cls, value: str) -> str:
        try:
            Frequency(value)
        except ValueError as exc:
            raise ValueError(f"Unrecognized frequency: {value}") from exc
        return value

    def to_domain_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "frequency": Frequency(self.frequency),
            "start_date": self.start_date,
        }


class ReplaceCartRequestDto(BaseModel):
    items: list[ReplaceCartItemDto]
    if_version: int = Field(alias="ifVersion")

    model_config = {"populate_by_name": True}


def serialize_line_item(line_item: LineItem) -> dict[str, Any]:
    return {
        "id": line_item.id,
        "productId": line_item.product_id,
        "quantity": line_item.quantity,
        "frequency": str(line_item.frequency),
        "startDate": line_item.start_date,
        "addedAt": line_item.added_at,
    }


def serialize_quote(quote: Quote) -> dict[str, Any]:
    return {
        "basePrice": quote.base_price,
        "taxAmount": quote.tax_amount,
        "taxRate": quote.tax_rate,
        "deliveryFee": quote.delivery_fee,
        "netPayable": quote.net_payable,
        "monthlyEstimate": quote.monthly_estimate,
        "discountAmount": quote.discount_amount,
        "appliedOfferId": quote.applied_offer_id,
    }


def serialize_cart_view(view: CartView) -> dict[str, Any]:
    return {
        "items": [serialize_line_item(li) for li in view.cart.line_items],
        "cartVersion": view.cart.cart_version,
        "quote": serialize_quote(view.quote) if view.quote is not None else None,
    }


def serialize_cart(cart: Cart) -> dict[str, Any]:
    # PUT /cart's own response (FR-3) — no live quote here, matching
    # get_cart's contract not being duplicated on every write; a caller
    # that needs the updated pricing breakdown calls GET /cart, same
    # "events/writes signal, GET /cart serves state" split as FR-5's own
    # thin CartUpdated event.
    return {
        "items": [serialize_line_item(li) for li in cart.line_items],
        "cartVersion": cart.cart_version,
    }


def success_response(data: Any, status_code: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"requestId": str(uuid.uuid4()), "status": "success", "data": data}),
    }


def no_content_response() -> dict[str, Any]:
    return {"statusCode": 204, "headers": {}, "body": ""}


def error_response(exc: CartServiceError) -> dict[str, Any]:
    # `details` spread after the canonical keys, never into them — an
    # upstream error payload forwarded as `details` must never silently
    # overwrite this service's own error_code/message.
    safe_details = {k: v for k, v in exc.details.items() if k not in ("errorCode", "message")}
    return {
        "statusCode": exc.http_status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "requestId": str(uuid.uuid4()),
                "status": "error",
                "data": {"errorCode": exc.error_code, "message": exc.message, **safe_details},
            }
        ),
    }


def validation_error_response(message: str) -> dict[str, Any]:
    return error_response(ValidationError(message))
