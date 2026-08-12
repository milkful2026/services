import pytest
from redis.exceptions import RedisError

from adapters.rate_limit_adapter import RedisRateLimiterAdapter
from domain.exceptions import ExternalServiceUnavailableError, RateLimitExceededError


@pytest.fixture
def adapter(fake_redis):
    return RedisRateLimiterAdapter(redis_client=fake_redis)


def test_allows_requests_under_the_limit(adapter):
    for _ in range(3):
        adapter.check_and_increment("register:otp:+919876543210", max_requests=3, window_seconds=900)


def test_raises_once_limit_exceeded(adapter):
    for _ in range(3):
        adapter.check_and_increment("register:otp:+919876543210", max_requests=3, window_seconds=900)

    with pytest.raises(RateLimitExceededError):
        adapter.check_and_increment("register:otp:+919876543210", max_requests=3, window_seconds=900)


def test_sets_expiry_only_on_first_increment(adapter, fake_redis):
    key = "register:otp:+919876543210"
    adapter.check_and_increment(key, max_requests=3, window_seconds=900)
    ttl_after_first = fake_redis.ttl(key)

    adapter.check_and_increment(key, max_requests=3, window_seconds=900)
    ttl_after_second = fake_redis.ttl(key)

    assert ttl_after_first > 0
    # Second call must not reset the window back to 900.
    assert ttl_after_second <= ttl_after_first


def test_counters_are_isolated_per_key(adapter):
    for _ in range(3):
        adapter.check_and_increment("register:otp:+919876543210", max_requests=3, window_seconds=900)

    # A different mobile's counter must be unaffected.
    adapter.check_and_increment("register:otp:+919999999999", max_requests=3, window_seconds=900)


def test_wraps_redis_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisError("connection refused")

    monkeypatch.setattr(adapter._redis, "incr", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.check_and_increment("register:otp:+919876543210", max_requests=3, window_seconds=900)


def test_self_heals_a_counter_left_without_ttl(adapter, fake_redis):
    # Simulate the bug this guards against: a prior expire() call failed
    # (e.g. transient Redis error) after incr() succeeded, leaving the
    # counter with no TTL — it must not be stuck that way forever.
    key = "register:otp:+919876543210"
    fake_redis.incr(key)
    fake_redis.persist(key)
    assert fake_redis.ttl(key) < 0

    adapter.check_and_increment(key, max_requests=3, window_seconds=900)

    assert fake_redis.ttl(key) > 0
