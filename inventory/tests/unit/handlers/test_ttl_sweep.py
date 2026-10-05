import pytest

from handlers import ttl_sweep


class FakeService:
    def __init__(self, released):
        self._released = released

    def run_ttl_sweep(self):
        return self._released


def test_run_once_returns_count_of_released_reservations():
    service = FakeService(released=["r1", "r2", "r3"])

    count = ttl_sweep.run_once(service)

    assert count == 3


def test_run_once_builds_a_service_when_none_given(monkeypatch):
    built = FakeService(released=[])
    monkeypatch.setattr(ttl_sweep, "_build_service", lambda: built)

    count = ttl_sweep.run_once()

    assert count == 0


def test_run_forever_logs_and_continues_on_a_tick_failure(monkeypatch):
    calls = {"n": 0}

    def _raise_once_then_stop(service):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        raise SystemExit  # stop the otherwise-infinite loop after tick 2

    monkeypatch.setattr(ttl_sweep, "_build_service", lambda: object())
    monkeypatch.setattr(ttl_sweep, "run_once", _raise_once_then_stop)
    monkeypatch.setattr(ttl_sweep.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit):
        ttl_sweep.run_forever()

    assert calls["n"] == 2  # survived the first RuntimeError, reached tick 2
