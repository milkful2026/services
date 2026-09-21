"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap, same as catalog/inventory/wallet). No real AWS,
DB, or network.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.subscription_repository import SqlAlchemySubscriptionRepository, create_schema
from config.env import Settings
from domain.subscription_service import SubscriptionService


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SUBSCRIPTION_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("SUBSCRIPTION_EVENT_BUS_NAME", "milkful-events")
    monkeypatch.setenv("SUBSCRIPTION_CATALOG_BASE_URL", "http://catalog.test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def repo(engine):
    return SqlAlchemySubscriptionRepository(engine)


class FakeCatalogClient:
    """Configurable stand-in for HttpCatalogClient — no real HTTP."""

    def __init__(self):
        self.products: dict[str, dict] = {}
        self.raise_unavailable = False

    def seed(self, product_id: str, *, subscription_eligible: bool = True) -> None:
        self.products[product_id] = {
            "id": product_id,
            "subscriptionEligible": subscription_eligible,
        }

    def get_product(self, product_id: str) -> dict | None:
        if self.raise_unavailable:
            from domain.exceptions import CatalogUnavailableError

            raise CatalogUnavailableError("Catalog unavailable (fake)")
        return self.products.get(product_id)


@pytest.fixture
def catalog_client():
    client = FakeCatalogClient()
    client.seed("prod-1", subscription_eligible=True)
    return client


@pytest.fixture
def service(repo, catalog_client, settings):
    return SubscriptionService(repo, catalog_client, settings)
