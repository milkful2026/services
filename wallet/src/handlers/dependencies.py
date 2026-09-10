"""FastAPI dependency wiring — the composition root. `lru_cache` gives a
per-process singleton; tests override via `app.dependency_overrides`."""

from functools import lru_cache

from sqlalchemy import create_engine

from adapters.wallet_repository import SqlAlchemyWalletRepository
from config.env import get_settings
from domain.wallet_service import WalletService


@lru_cache
def get_wallet_service() -> WalletService:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyWalletRepository(engine)
    return WalletService(repository, settings)
