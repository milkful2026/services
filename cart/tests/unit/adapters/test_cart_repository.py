import pytest

from adapters.cart_repository import DynamoDbCartRepository
from domain.exceptions import CartVersionMismatchError, LineItemNotFoundError
from domain.models import Frequency


@pytest.fixture
def repo(cart_table):
    return DynamoDbCartRepository(table_name="cart", region_name="ap-south-1", event_source="cart")


def test_get_cart_returns_empty_cart_when_none_exists(repo):
    cart = repo.get_cart("user-1")

    assert cart.line_items == []
    assert cart.cart_version == 0


def test_add_item_then_get_cart_round_trip(repo):
    line_item = repo.add_item(
        "user-1", "cow-milk", quantity=2, frequency=Frequency.ONE_TIME, start_date=None,
        idempotency_key=None,
    )

    cart = repo.get_cart("user-1")

    assert cart.cart_version == 1
    assert len(cart.line_items) == 1
    fetched = cart.line_items[0]
    assert fetched.id == line_item.id
    assert fetched.product_id == "cow-milk"
    assert fetched.quantity == 2
    assert fetched.frequency == Frequency.ONE_TIME


def test_add_item_increments_cart_version_each_time(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)
    repo.add_item("user-1", "cow-ghee", 1, Frequency.ONE_TIME, None, None)

    cart = repo.get_cart("user-1")

    assert cart.cart_version == 2
    assert len(cart.line_items) == 2


def test_add_item_two_distinct_line_items_for_same_product(repo):
    # FR-2: a caller may have more than one line item for the same
    # product with different configurations — must not collapse.
    first = repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)
    second = repo.add_item("user-1", "cow-milk", 1, Frequency.DAILY, "2026-09-01", None)

    assert first.id != second.id
    cart = repo.get_cart("user-1")
    assert len(cart.line_items) == 2


def test_add_item_idempotency_key_replay_returns_original_without_duplicate(repo):
    first = repo.add_item(
        "user-1", "cow-milk", 2, Frequency.ONE_TIME, None, idempotency_key="key-1"
    )

    replay = repo.add_item(
        "user-1", "cow-milk", 2, Frequency.ONE_TIME, None, idempotency_key="key-1"
    )

    assert replay.id == first.id
    cart = repo.get_cart("user-1")
    assert len(cart.line_items) == 1  # no duplicate line item
    assert cart.cart_version == 1  # no duplicate version bump either


def test_add_item_different_idempotency_keys_both_apply(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, idempotency_key="key-1")
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, idempotency_key="key-2")

    cart = repo.get_cart("user-1")
    assert len(cart.line_items) == 2


def test_delete_item_removes_it_and_bumps_version(repo):
    added = repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)

    repo.delete_item("user-1", added.id)

    cart = repo.get_cart("user-1")
    assert cart.line_items == []
    assert cart.cart_version == 2


def test_delete_item_missing_raises_not_found(repo):
    with pytest.raises(LineItemNotFoundError):
        repo.delete_item("user-1", "does-not-exist")


def test_delete_item_belonging_to_another_user_raises_not_found(repo):
    added = repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)

    with pytest.raises(LineItemNotFoundError):
        repo.delete_item("user-2", added.id)


def test_replace_cart_full_replace_removes_omitted_items(repo):
    kept = repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)
    repo.add_item("user-1", "cow-ghee", 1, Frequency.ONE_TIME, None, None)  # will be omitted

    new_cart = repo.replace_cart(
        "user-1",
        items=[
            {"id": kept.id, "product_id": "cow-milk", "quantity": 3,
             "frequency": Frequency.ONE_TIME},
            {"product_id": "paneer", "quantity": 1, "frequency": Frequency.ONE_TIME},
        ],
        if_version=2,
    )

    products = {li.product_id for li in new_cart.line_items}
    assert products == {"cow-milk", "paneer"}
    kept_item = next(li for li in new_cart.line_items if li.product_id == "cow-milk")
    assert kept_item.quantity == 3
    assert new_cart.cart_version == 3


def test_replace_cart_on_a_fresh_never_existed_cart_requires_version_zero(repo):
    cart = repo.replace_cart(
        "user-1",
        items=[{"product_id": "cow-milk", "quantity": 1, "frequency": Frequency.ONE_TIME}],
        if_version=0,
    )

    assert len(cart.line_items) == 1
    assert cart.cart_version == 1


def test_replace_cart_stale_version_raises_conflict(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)  # version -> 1

    with pytest.raises(CartVersionMismatchError):
        repo.replace_cart(
            "user-1",
            items=[{"product_id": "paneer", "quantity": 1, "frequency": Frequency.ONE_TIME}],
            if_version=0,  # stale — actual version is 1
        )

    # The rejected write must not have partially applied.
    cart = repo.get_cart("user-1")
    assert len(cart.line_items) == 1
    assert cart.line_items[0].product_id == "cow-milk"
    assert cart.cart_version == 1


def test_replace_cart_stale_version_on_never_existed_cart_raises_conflict(repo):
    with pytest.raises(CartVersionMismatchError):
        repo.replace_cart(
            "user-1",
            items=[{"product_id": "cow-milk", "quantity": 1, "frequency": Frequency.ONE_TIME}],
            if_version=1,  # no cart exists yet — only 0 would be valid
        )


def test_outbox_row_written_alongside_add_item(cart_table, repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)

    response = cart_table.query(
        KeyConditionExpression="userId = :uid",
        ExpressionAttributeValues={":uid": "user-1"},
    )
    outbox_rows = [i for i in response["Items"] if i["SK"].startswith("OUTBOX#")]
    assert len(outbox_rows) == 1
    assert outbox_rows[0]["type"] == "CartUpdated"


def test_meta_row_gets_its_own_ttl(cart_table, repo):
    # Fixes specs PR #11's re-flagged finding: META's cartVersion must not
    # persist forever once everything else has expired.
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)

    response = cart_table.get_item(Key={"userId": "user-1", "SK": "META"})
    assert "expiresAt" in response["Item"]


def test_get_unpublished_outbox_events_returns_events_from_writes(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)
    repo.add_item("user-2", "paneer", 1, Frequency.ONE_TIME, None, None)

    events = repo.get_unpublished_outbox_events(limit=10)

    assert len(events) == 2
    user_ids = {e["userId"] for e in events}
    assert user_ids == {"user-1", "user-2"}
    for e in events:
        assert e["type"] == "CartUpdated"
        assert e["payload"]["changeType"] == "ITEM_ADDED"
        assert "eventId" in e


def test_mark_outbox_published_excludes_it_from_future_reads(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)
    [event] = repo.get_unpublished_outbox_events(limit=10)

    repo.mark_outbox_published(event["userId"], event["eventId"])

    assert repo.get_unpublished_outbox_events(limit=10) == []


def test_get_unpublished_outbox_events_ignores_item_and_meta_rows(repo):
    repo.add_item("user-1", "cow-milk", 1, Frequency.ONE_TIME, None, None)

    events = repo.get_unpublished_outbox_events(limit=10)

    assert len(events) == 1  # not the ITEM# row, not the META row too
