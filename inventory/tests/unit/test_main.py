import pytest

import main
from handlers.health import consumer_health


@pytest.fixture(autouse=True)
def _reset_consumer_health():
    consumer_health.alive = True
    yield
    consumer_health.alive = True


def test_run_consumer_flips_health_unhealthy_and_reraises_on_crash(monkeypatch):
    def _raise_on_get_settings():
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_settings", _raise_on_get_settings)

    with pytest.raises(RuntimeError):
        main._run_consumer()

    assert consumer_health.alive is False
