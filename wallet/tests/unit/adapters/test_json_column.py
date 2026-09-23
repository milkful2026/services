"""Dialect-level coverage for shared.adapters.json_column.JSONColumn,
used by the outbox `payload` column.

Regression: the previous approach (`JSONB().with_variant(Text, "sqlite")`
plus a manual, unconditional `json.dumps()` before every insert) worked
on SQLite by accident but double-encoded on real Postgres — json.dumps()
pre-serialized the dict to a string, and then Postgres's own JSONB bind
processor serialized *that string* again, storing a JSON-string scalar
instead of a JSON object. Invisible here because this whole suite runs
against SQLite, never real Postgres — these tests exercise the type
decorator directly against both dialects' `Dialect` objects (no live
connection needed) so the Postgres behavior is actually covered."""

from shared.adapters.json_column import JSONColumn
from sqlalchemy.dialects import postgresql, sqlite

_POSTGRES = postgresql.dialect()
_SQLITE = sqlite.dialect()


def test_postgres_bind_param_passes_dict_through_unserialized():
    # No pre-serialization before binding to a real JSONB column — this
    # is the bug: json.dumps()-ing here would double-encode once
    # SQLAlchemy's own JSONB bind processor serializes it again.
    bound = JSONColumn().process_bind_param({"a": 1}, _POSTGRES)
    assert bound == {"a": 1}
    assert isinstance(bound, dict)


def test_sqlite_bind_param_serializes_to_json_string():
    # SQLite's Text column can't bind a raw dict, so this dialect does
    # need the manual serialization.
    bound = JSONColumn().process_bind_param({"a": 1}, _SQLITE)
    assert bound == '{"a": 1}'
    assert isinstance(bound, str)


def test_bind_param_passes_none_through_on_both_dialects():
    assert JSONColumn().process_bind_param(None, _POSTGRES) is None
    assert JSONColumn().process_bind_param(None, _SQLITE) is None


def test_result_value_round_trips_on_both_dialects():
    assert JSONColumn().process_result_value('{"a": 1}', _SQLITE) == {"a": 1}
    assert JSONColumn().process_result_value({"a": 1}, _POSTGRES) == {"a": 1}


def test_result_value_none_on_both_dialects():
    assert JSONColumn().process_result_value(None, _POSTGRES) is None
    assert JSONColumn().process_result_value(None, _SQLITE) is None
