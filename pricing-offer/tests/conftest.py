"""Shared pytest fixtures. No DB/AWS credentials needed at all in this
build — the only shared setup is making sure Settings() doesn't pick up
a developer's own real environment variables mid-test-run."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PRICING_CATALOG_BASE_URL", "http://catalog.test")
    monkeypatch.setenv("PRICING_DEFAULT_TAX_RATE_PERCENT", "5.0")
    monkeypatch.setenv("PRICING_DELIVERY_FEE", "20.0")
    yield
