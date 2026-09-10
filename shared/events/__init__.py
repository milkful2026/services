"""Authoritative EventBridge `detail` contracts.

These JSON Schema files are the single source of truth for the domain
events crossing the `milkful-events` bus for MA-24 (wallet recharge).
Both the producer (Payment Service, MA-126) and the consumer (Wallet
Service, MA-127) validate against them in their test suites.

Per `services/README.md` §2, `shared/` holds transport/envelope helpers
only — no business rules. A schema definition qualifies.
"""

import json
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent


@lru_cache
def load_schema(name: str) -> dict:
    """Load a `*.schema.json` from this package by its title, e.g.
    `load_schema("PaymentConfirmed")`."""
    path = _HERE / f"{name}.schema.json"
    if not path.is_file():
        raise FileNotFoundError(f"no event schema: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
