import pytest

from adapters.admin_challenge_store_adapter import AdminChallengeStoreAdapter
from domain.admin_models import LoginChallenge


@pytest.fixture
def store(fake_redis):
    return AdminChallengeStoreAdapter(redis_client=fake_redis)


def test_put_then_get_round_trips(store):
    challenge = LoginChallenge(
        challenge_token="tok-1",
        admin_id="admin-1",
        email="a@milkful.test",
        cognito_session="session-1",
        expires_at=0,
    )

    store.put(challenge, ttl_seconds=300)
    found = store.get("tok-1")

    assert found is not None
    assert found.admin_id == "admin-1"
    assert found.email == "a@milkful.test"
    assert found.cognito_session == "session-1"


def test_get_returns_none_for_unknown_token(store):
    assert store.get("no-such-token") is None


def test_consume_deletes_the_challenge(store):
    challenge = LoginChallenge(
        challenge_token="tok-1", admin_id="admin-1", email="a@milkful.test", cognito_session="s", expires_at=0
    )
    store.put(challenge, ttl_seconds=300)

    store.consume("tok-1")

    assert store.get("tok-1") is None


def test_put_sets_a_ttl_on_the_key(store, fake_redis):
    challenge = LoginChallenge(
        challenge_token="tok-1", admin_id="admin-1", email="a@milkful.test", cognito_session="s", expires_at=0
    )
    store.put(challenge, ttl_seconds=300)

    assert fake_redis.ttl("admin:challenge:tok-1") > 0
