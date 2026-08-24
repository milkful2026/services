"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5 — identical to every other service's own
`{requestId, status, data}` shape."""

import uuid
from typing import Any

from domain.models import Quote


def serialize_quote(quote: Quote) -> dict[str, Any]:
    # Matches lib/features/cart/models/quote.dart's Quote.fromJson exactly.
    # discountAmount/appliedOfferId are always null in this build — no
    # Offers system exists (see README's "Scope" section), and the mobile
    # client already treats both as optional (`num?`).
    return {
        "basePrice": quote.base_price,
        "taxAmount": quote.tax_amount,
        "taxRate": quote.tax_rate,
        "deliveryFee": quote.delivery_fee,
        "netPayable": quote.net_payable,
        "monthlyEstimate": quote.monthly_estimate,
        "discountAmount": None,
        "appliedOfferId": None,
    }


def success_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "status": "success", "data": data}


def error_envelope(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "status": "error",
        "data": {"errorCode": error_code, "message": message, **(details or {})},
    }
