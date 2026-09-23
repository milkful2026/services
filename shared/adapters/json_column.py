"""Dialect-aware JSON column TypeDecorator: JSONB on Postgres, JSON-in-Text
on SQLite, exactly-once serialization either way.

Was hand-duplicated byte-for-byte as `_JSONColumn` in subscription/order's
own adapters/*_repository.py — moved here per services/README.md §2.
`with_variant(Text, "sqlite")` on a plain `JSONB` column alone isn't
enough: SQLite's plain `Text` can't bind a raw dict, so it needs
`json.dumps`/`json.loads` on the Python side — but doing that
unconditionally *and* also using `JSONB` (which applies its own
dict<->jsonb serialization) double-encodes every value into a JSON string
scalar on Postgres. This was a live bug in wallet's and payment's own
outbox `payload` columns (and payment's `raw_payload`), which hand-rolled
`json.dumps()` before binding to a `JSONB().with_variant(Text, "sqlite")`
column — fixed by switching to this type. Dialect-aware here so callers
just pass/receive plain dicts on both dialects.
"""

import json

from sqlalchemy import Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB


class JSONColumn(TypeDecorator):
    impl = JSONB
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(JSONB())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str) else value
