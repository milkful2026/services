import pytest
from redis.exceptions import RedisError

from adapters.rate_limit_adapter import RedisLockAdapter
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def adapter(fake_redis):
    return RedisLockAdapter(redis_client=fake_redis)


def test_acquire_succeeds_when_key_is_free(adapter):
    assert adapter.acquire("register:otp:lock:+919876543210", ttl_seconds=10) is True


def test_acquire_fails_while_held(adapter):
    key = "register:otp:lock:+919876543210"
    adapter.acquire(key, ttl_seconds=10)

    assert adapter.acquire(key, ttl_seconds=10) is False


def test_acquire_succeeds_again_after_release(adapter):
    key = "register:otp:lock:+919876543210"
    adapter.acquire(key, ttl_seconds=10)
    adapter.release(key)

    assert adapter.acquire(key, ttl_seconds=10) is True


def test_acquire_wraps_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "set", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.acquire("register:otp:lock:+919876543210", ttl_seconds=10)


def test_release_swallows_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "delete", _raise)

    adapter.release("register:otp:lock:+919876543210")  # must not raise
