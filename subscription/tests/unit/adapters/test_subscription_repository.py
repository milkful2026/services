from datetime import date

from domain.models import Schedule, ScheduleType, Subscription, SubscriptionStatus


def _sub(**overrides) -> Subscription:
    defaults = dict(
        id="sub_1",
        user_id="user-1",
        product_id="prod-1",
        quantity=1,
        schedule=Schedule(type=ScheduleType.DAILY),
        slot_id="slot-1",
        status=SubscriptionStatus.ACTIVE,
        start_date=date(2026, 1, 15),
    )
    defaults.update(overrides)
    return Subscription(**defaults)


class TestInsertIfAbsent:
    def test_fresh_insert_returns_created_true(self, repo):
        sub, created = repo.insert_if_absent(
            subscription=_sub(),
            idempotency_key="key-1",
            same_day_delivery_date=None,
            outbox_event_type=None,
            outbox_payload=None,
        )
        assert created is True
        assert repo.get_by_id(sub.id) is not None

    def test_concurrent_duplicate_key_returns_existing_not_a_second_row(self, repo):
        first, _ = repo.insert_if_absent(
            subscription=_sub(id="sub_1"),
            idempotency_key="key-1",
            same_day_delivery_date=None,
            outbox_event_type=None,
            outbox_payload=None,
        )
        # Simulates the narrow race window: a second concurrent attempt
        # with a *different* generated subscription id but the same
        # (user_id, idempotency_key) — the UNIQUE constraint must win,
        # and the repo must return the winner, not raise.
        second, created = repo.insert_if_absent(
            subscription=_sub(id="sub_2"),
            idempotency_key="key-1",
            same_day_delivery_date=None,
            outbox_event_type=None,
            outbox_payload=None,
        )
        assert created is False
        assert second.id == first.id
        assert repo.get_by_id("sub_2") is None

    def test_same_day_delivery_inserts_run_log_and_outbox_atomically(self, repo):
        sub, _ = repo.insert_if_absent(
            subscription=_sub(),
            idempotency_key="key-1",
            same_day_delivery_date=date(2026, 1, 15),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload={"subscriptionId": "sub_1", "deliveryDate": "2026-01-15"},
        )
        assert repo.list_logged_subscription_ids(date(2026, 1, 15)) == {sub.id}
        unpub = repo.fetch_unpublished()
        assert len(unpub) == 1
        assert unpub[0]["event_type"] == "SubscriptionOrderDue"


class TestInsertRunLogAndEnqueue:
    def test_fresh_insert_returns_true_and_enqueues_one_event(self, repo):
        repo.insert_if_absent(
            subscription=_sub(),
            idempotency_key="key-1",
            same_day_delivery_date=None,
            outbox_event_type=None,
            outbox_payload=None,
        )
        emitted = repo.insert_run_log_and_enqueue(
            subscription_id="sub_1",
            delivery_date=date(2026, 1, 16),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload={"subscriptionId": "sub_1", "deliveryDate": "2026-01-16"},
        )
        assert emitted is True
        assert len(repo.fetch_unpublished()) == 1

    def test_duplicate_delivery_date_returns_false_no_second_event(self, repo):
        repo.insert_if_absent(
            subscription=_sub(),
            idempotency_key="key-1",
            same_day_delivery_date=None,
            outbox_event_type=None,
            outbox_payload=None,
        )
        payload = {"subscriptionId": "sub_1", "deliveryDate": "2026-01-16"}
        first = repo.insert_run_log_and_enqueue(
            subscription_id="sub_1",
            delivery_date=date(2026, 1, 16),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload=payload,
        )
        second = repo.insert_run_log_and_enqueue(
            subscription_id="sub_1",
            delivery_date=date(2026, 1, 16),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload=payload,
        )
        assert first is True
        assert second is False
        assert len(repo.fetch_unpublished()) == 1


class TestListLoggedSubscriptionIds:
    def test_batched_across_multiple_subscriptions(self, repo):
        repo.insert_if_absent(
            subscription=_sub(id="sub_1"),
            idempotency_key="key-1",
            same_day_delivery_date=date(2026, 1, 16),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload={},
        )
        repo.insert_if_absent(
            subscription=_sub(id="sub_2", user_id="user-2"),
            idempotency_key="key-2",
            same_day_delivery_date=date(2026, 1, 16),
            outbox_event_type="SubscriptionOrderDue",
            outbox_payload={},
        )
        repo.insert_if_absent(
            subscription=_sub(id="sub_3", user_id="user-3"),
            idempotency_key="key-3",
            same_day_delivery_date=None,  # not logged for this date
            outbox_event_type=None,
            outbox_payload=None,
        )
        assert repo.list_logged_subscription_ids(date(2026, 1, 16)) == {"sub_1", "sub_2"}
