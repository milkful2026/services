"""Shared composition root for the four request-handling handlers
(get_cart, add_item, put_cart, delete_item) — keeps the dependency graph
from drifting between copies. outbox_publisher_handler wires a different,
smaller graph (repository + EventBridge publisher only) and isn't a fit
for this helper, same split as services/user's own composition.py.
"""

from adapters.cart_repository import DynamoDbCartRepository
from adapters.catalog_client_adapter import HttpCatalogClient
from adapters.pricing_client_adapter import HttpPricingClient
from adapters.user_client_adapter import HttpUserClient
from adapters.wallet_client_adapter import HttpWalletClient
from config.env import Settings
from domain.cart_service import CartService


def build_cart_service(settings: Settings) -> CartService:
    repository = DynamoDbCartRepository(
        settings.cart_table_name, settings.aws_region, settings.event_source
    )
    catalog_client = HttpCatalogClient(
        settings.catalog_internal_base_url, settings.request_timeout_seconds
    )
    user_client = HttpUserClient(
        settings.user_internal_base_url, settings.aws_region, settings.request_timeout_seconds
    )
    pricing_client = HttpPricingClient(
        settings.pricing_internal_base_url, settings.request_timeout_seconds
    )
    wallet_client = HttpWalletClient()
    return CartService(
        repository, catalog_client, user_client, pricing_client, wallet_client,
        settings.wallet_minimum_balance,
    )
