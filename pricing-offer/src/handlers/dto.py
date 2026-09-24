"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5 — identical to every other service's own
`{requestId, status, data}` shape."""

from typing import Any

from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401

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
