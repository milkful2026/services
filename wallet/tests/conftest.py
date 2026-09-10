"""Shared pytest fixtures. SQLite in-memory stands in for Aurora Postgres
(documented fidelity gap, same as catalog/inventory). moto for SQS/events.
No real AWS, DB, or network.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from adapters.wallet_repository import (
    SqlAlchemyWalletRepository,
    create_schema,
    ledger_entries_table,
    wallets_table,
)
from config.env import Settings
from domain.wallet_service import WalletService


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WALLET_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("WALLET_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("WALLET_EVENT_BUS_NAME", "milkful-events")
    monkeypatch.setenv("WALLET_EVENTS_QUEUE_URL", "https://sqs.test/wallet-events-q")
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
    return SqlAlchemyWalletRepository(engine)


@pytest.fixture
def service(repo, settings):
    return WalletService(repo, settings)


def seed_wallet(engine, *, user_id="user-1", wallet_id="wal_1", balance_paise=0, status="ACTIVE"):
    with engine.begin() as conn:
        conn.execute(
            wallets_table.insert().values(
                id=wallet_id,
                user_id=user_id,
                balance_paise=balance_paise,
                currency="INR",
                status=status,
            )
        )
        conn.execute(
            ledger_entries_table.insert().values(
                wallet_id=wallet_id,
                type="OPENING",
                amount_paise=0,
                balance_after_paise=0,
                ref=f"opening:{wallet_id}",
                correlation_id=None,
            )
        )
    return {"user_id": user_id, "wallet_id": wallet_id}
