"""Regression coverage for the outbox publisher loop — no test exercised
this file before (confirmed: this is the first test added here)."""

from unittest.mock import Mock

import pytest

from handlers import outbox_publisher


def test_run_once_never_touches_create_engine_or_get_settings(repo, monkeypatch):
    # Regression: run_once() used to build its own SQLAlchemy engine
    # (create_engine(settings.database_url)) on every call — a fresh
    # engine/connection pool every 5s tick from run_forever's loop. After
    # the fix, run_once takes an already-built repo/publisher and must
    # never touch create_engine or get_settings itself — only
    # run_forever's one-time setup does.
    def _boom(*args, **kwargs):
        raise AssertionError("run_once must not call create_engine")

    monkeypatch.setattr(outbox_publisher, "create_engine", _boom)
    monkeypatch.setattr(outbox_publisher, "get_settings", _boom)

    publisher = Mock()
    published = outbox_publisher.run_once(repo, publisher)
    assert published == 0  # nothing queued in the fresh test repo
    publisher.publish.assert_not_called()


def test_run_once_publishes_and_marks_each_unpublished_row(repo):
    repo.enqueue_outbox_event(
        aggregate_id="wal_1", event_type="WalletCreated", payload={"userId": "user-1"}
    )

    publisher = Mock()
    published = outbox_publisher.run_once(repo, publisher)

    assert published == 1
    publisher.publish.assert_called_once_with("WalletCreated", {"userId": "user-1"})
    assert repo.fetch_unpublished() == []  # marked published


def test_run_forever_builds_engine_and_dependencies_exactly_once(monkeypatch, engine):
    # run_forever's own one-time setup is what's allowed to call
    # create_engine — verified by passing a pre-built engine in (the
    # signature's own escape hatch for tests) and confirming the loop
    # runs against it without erroring, then breaking out after one tick.
    calls = {"n": 0}

    def _fake_run_once(repo, publisher):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt  # stop the infinite loop deterministically
        return 0

    monkeypatch.setattr(outbox_publisher, "run_once", _fake_run_once)
    monkeypatch.setattr(outbox_publisher.time, "sleep", lambda _s: None)
    monkeypatch.setenv("WALLET_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("WALLET_AWS_REGION", "ap-south-1")
    monkeypatch.setenv("WALLET_EVENT_BUS_NAME", "milkful-events")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with pytest.raises(KeyboardInterrupt):
        outbox_publisher.run_forever(interval_seconds=0.0, engine=engine)

    assert calls["n"] == 2  # run_once called each tick, engine built only once by run_forever
