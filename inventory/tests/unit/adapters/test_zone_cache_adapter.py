import pytest
from redis.exceptions import RedisError

from adapters.zone_cache_adapter import RedisZoneCacheAdapter


@pytest.fixture
def adapter(fake_redis):
    return RedisZoneCacheAdapter(redis_client=fake_redis)


def test_get_returns_none_when_not_cached(adapter):
    assert adapter.get("560001") is None


def test_set_then_get_round_trips(adapter):
    result = {"serviceable": True, "zoneId": "blr-central", "zoneName": "Bangalore Central",
              "slots": [], "message": None, "waitlistAvailable": False}

    adapter.set("560001", result, ttl_seconds=900)

    assert adapter.get("560001") == result


def test_set_uses_key_prefix_and_ttl(adapter, fake_redis):
    adapter.set("560001", {"serviceable": False}, ttl_seconds=900)

    assert fake_redis.exists("svc:560001")
    assert fake_redis.ttl("svc:560001") > 0


def test_invalidate_removes_key(adapter):
    adapter.set("560001", {"serviceable": True}, ttl_seconds=900)
    adapter.invalidate("560001")

    assert adapter.get("560001") is None


def test_get_swallows_redis_error_and_returns_none(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "get", _raise)

    assert adapter.get("560001") is None


def test_set_swallows_redis_error_without_raising(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "set", _raise)

    adapter.set("560001", {"serviceable": True}, ttl_seconds=900)  # must not raise


def test_invalidate_by_prefix_removes_all_matching_keys(adapter):
    adapter.set("560001", {"serviceable": True}, ttl_seconds=900)
    adapter.set("560099", {"serviceable": True}, ttl_seconds=900)
    adapter.set("110001", {"serviceable": False}, ttl_seconds=900)  # different prefix

    adapter.invalidate_by_prefix("5600")

    assert adapter.get("560001") is None
    assert adapter.get("560099") is None
    assert adapter.get("110001") is not None


def test_invalidate_by_prefix_swallows_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "scan_iter", _raise)

    adapter.invalidate_by_prefix("5600")  # must not raise
