import pytest

import handlers.outbox_publisher_handler as outbox_publisher_handler
from domain.exceptions import ExternalServiceUnavailableError


class FakeSettings:
    outbox_batch_size = 25


class FakeRepository:
    def __init__(self, events=None, mark_fail_ids: set[str] | None = None):
        self.events = events or []
        self.marked_published: list[str] = []
        self.mark_fail_ids = mark_fail_ids or set()
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_unpublished_outbox_events(self, limit):
        return self.events

    def mark_outbox_published(self, event_id):
        if event_id in self.mark_fail_ids:
            raise ExternalServiceUnavailableError("mark failed")
        self.marked_published.append(event_id)


class FakePublisher:
    def __init__(self, fail_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.published: list[tuple] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def publish(self, event_type, payload, correlation_id):
        # Use payload's marker to decide failure, set by tests via a
        # special "id" key threaded through for simplicity.
        if payload.get("_id") in self.fail_ids:
            raise ExternalServiceUnavailableError("publish failed")
        self.published.append((event_type, payload, correlation_id))


@pytest.fixture(autouse=True)
def _reset_deps():
    outbox_publisher_handler._deps = None
    yield
    outbox_publisher_handler._deps = None


def _inject(events=None, fail_ids=None, mark_fail_ids=None):
    repo = FakeRepository(events=events, mark_fail_ids=mark_fail_ids)
    publisher = FakePublisher(fail_ids=fail_ids)
    outbox_publisher_handler._deps = {
        "repository": repo,
        "publisher": publisher,
        "settings": FakeSettings(),
    }
    return repo, publisher


def test_publishes_all_unpublished_events_and_marks_them():
    repo, publisher = _inject(
        events=[
            {
                "id": "evt-1",
                "aggregateId": "user-1",
                "type": "UserRegistered",
                "payload": {"_id": "evt-1"},
            },
            {
                "id": "evt-2",
                "aggregateId": "user-2",
                "type": "UserRegistered",
                "payload": {"_id": "evt-2"},
            },
        ]
    )

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 2, "failedCount": 0}
    assert repo.marked_published == ["evt-1", "evt-2"]
    assert len(publisher.published) == 2


def test_no_unpublished_events_returns_zero_counts():
    _inject(events=[])

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 0}


def test_publish_failure_for_one_event_does_not_block_others():
    repo, publisher = _inject(
        events=[
            {
                "id": "evt-1",
                "aggregateId": "user-1",
                "type": "UserRegistered",
                "payload": {"_id": "evt-1"},
            },
            {
                "id": "evt-2",
                "aggregateId": "user-2",
                "type": "UserRegistered",
                "payload": {"_id": "evt-2"},
            },
        ],
        fail_ids={"evt-1"},
    )

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 1, "failedCount": 1}
    assert repo.marked_published == ["evt-2"]  # evt-1 left unpublished for retry


def test_mark_failure_after_successful_publish_is_counted_failed_not_silently_dropped():
    # The event WAS published (unlike a publish failure) — it must still
    # count as failed (so it's visible) and stay unmarked so it can be
    # retried, rather than crashing the batch or silently vanishing.
    repo, publisher = _inject(
        events=[
            {
                "id": "evt-1",
                "aggregateId": "user-1",
                "type": "UserRegistered",
                "payload": {"_id": "evt-1"},
            },
        ],
        mark_fail_ids={"evt-1"},
    )

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 1}
    assert len(publisher.published) == 1  # it really was published
    assert repo.marked_published == []  # but never marked -> will retry


def test_unexpected_exception_for_one_event_does_not_kill_the_batch():
    repo, publisher = _inject(
        events=[
            {
                "id": "evt-1",
                "aggregateId": "user-1",
                "type": "UserRegistered",
                "payload": {"_id": "evt-1"},
            },
            {
                "id": "evt-2",
                "aggregateId": "user-2",
                "type": "UserRegistered",
                "payload": {"_id": "evt-2"},
            },
        ]
    )

    def _raise(*args, **kwargs):
        raise KeyError("type")

    publisher.publish = _raise

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 2}
