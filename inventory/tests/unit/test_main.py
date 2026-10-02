import pytest

import main
from handlers.health import consumer_health


@pytest.fixture(autouse=True)
def _reset_consumer_health():
    consumer_health.alive = True
    yield
    consumer_health.alive = True


def test_run_zone_update_consumer_flips_health_unhealthy_and_reraises_on_crash(monkeypatch):
    def _raise_on_get_settings():
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_settings", _raise_on_get_settings)

    with pytest.raises(RuntimeError):
        main._run_zone_update_consumer()

    assert consumer_health.alive is False


@pytest.mark.parametrize(
    "target_name",
    ["_run_order_cancelled_consumer", "_run_catalog_updated_consumer"],
)
def test_other_consumer_threads_flip_health_unhealthy_and_reraise_on_crash(
    monkeypatch, target_name
):
    def _raise_on_get_settings():
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_settings", _raise_on_get_settings)

    with pytest.raises(RuntimeError):
        getattr(main, target_name)()

    assert consumer_health.alive is False


def test_run_ttl_sweep_thread_flips_health_unhealthy_and_reraises_on_crash(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main.ttl_sweep, "run_forever", _raise)

    with pytest.raises(RuntimeError):
        main._run_ttl_sweep()

    assert consumer_health.alive is False
