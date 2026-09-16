import pytest

from adapters.admin_session_registry import AdminSessionRegistryAdapter


@pytest.fixture
def registry(fake_redis):
    return AdminSessionRegistryAdapter(redis_client=fake_redis)


def test_register_session_no_eviction_when_under_limit(registry):
    evicted = registry.register_session("admin-1", "token-1", max_concurrent_sessions=3)

    assert evicted is None


def test_register_session_no_limit_never_evicts(registry):
    for i in range(10):
        evicted = registry.register_session("admin-1", f"token-{i}", max_concurrent_sessions=None)
        assert evicted is None


def test_register_session_evicts_oldest_when_over_limit(registry):
    registry.register_session("admin-1", "token-1", max_concurrent_sessions=2)
    registry.register_session("admin-1", "token-2", max_concurrent_sessions=2)
    evicted = registry.register_session("admin-1", "token-3", max_concurrent_sessions=2)

    assert evicted == "token-1"


def test_sessions_are_isolated_per_admin(registry):
    registry.register_session("admin-1", "token-1", max_concurrent_sessions=1)
    evicted = registry.register_session("admin-2", "token-2", max_concurrent_sessions=1)

    assert evicted is None


def test_invalidate_all_clears_tracked_sessions(registry):
    registry.register_session("admin-1", "token-1", max_concurrent_sessions=1)
    registry.invalidate_all("admin-1")

    # After invalidation, the list is empty, so a fresh registration
    # starts a new count from zero and shouldn't evict.
    evicted = registry.register_session("admin-1", "token-2", max_concurrent_sessions=1)
    assert evicted is None
