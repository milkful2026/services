import pytest
from redis.exceptions import RedisError

from adapters.admin_lockout_adapter import AdminLockoutAdapter
from domain.exceptions import ExternalServiceUnavailableError


@pytest.fixture
def adapter(fake_redis):
    return AdminLockoutAdapter(redis_client=fake_redis)


def test_record_failure_increments_and_sets_ttl(adapter, fake_redis):
    count1 = adapter.record_failure("admin-1", window_seconds=900)
    count2 = adapter.record_failure("admin-1", window_seconds=900)

    assert count1 == 1
    assert count2 == 2
    assert fake_redis.ttl("admin:2fa:failures:admin-1") > 0


def test_failure_counts_are_isolated_per_admin(adapter):
    adapter.record_failure("admin-1", window_seconds=900)
    count = adapter.record_failure("admin-2", window_seconds=900)

    assert count == 1


def test_is_locked_false_by_default(adapter):
    assert adapter.is_locked("admin-1") is False


def test_lock_then_is_locked_true(adapter):
    adapter.lock("admin-1", ttl_seconds=900)

    assert adapter.is_locked("admin-1") is True


def test_reset_clears_failures_and_lock(adapter):
    adapter.record_failure("admin-1", window_seconds=900)
    adapter.lock("admin-1", ttl_seconds=900)

    adapter.reset("admin-1")

    assert adapter.is_locked("admin-1") is False
    assert adapter.record_failure("admin-1", window_seconds=900) == 1


def test_record_failure_fails_closed_on_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("down")

    monkeypatch.setattr(adapter._redis, "incr", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.record_failure("admin-1", window_seconds=900)


def test_is_locked_fails_closed_on_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("down")

    monkeypatch.setattr(adapter._redis, "exists", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.is_locked("admin-1")
