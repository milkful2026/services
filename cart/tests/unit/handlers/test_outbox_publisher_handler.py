import pytest

import handlers.outbox_publisher_handler as outbox_publisher_handler
from domain.exceptions import CartServiceError


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

    def mark_outbox_published(self, user_id, event_id):
        if event_id in self.mark_fail_ids:
            raise CartServiceError("mark failed")
        self.marked_published.append(event_id)


class FakePublisher:
    def __init__(self, fail_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.published: list[tuple] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def publish(self, event_type, payload, correlation_id):
        if payload.get("eventId") in self.fail_ids:
            raise CartServiceError("publish failed")
        self.published.append((event_type, payload, correlation_id))


@pytest.fixture(autouse=True)
def _reset_deps():
    outbox_publisher_handler._deps = None
    yield
    outbox_publisher_handler._deps = None


def _inject(events=None, fail_ids=None, mark_fail_ids=None):
    repo = FakeRepository(events=events, mark_fail_ids=mark_fail_ids)
    publisher = FakePublisher(fail_ids=fail_ids)
    outbox_publisher_handler._deps = {"repository": repo, "publisher": publisher}
    return repo, publisher


def _event(event_id: str, user_id: str) -> dict:
    return {
        "userId": user_id,
        "eventId": event_id,
        "type": "CartUpdated",
        "payload": {"eventId": event_id, "userId": user_id, "changeType": "ITEM_ADDED"},
    }


def test_publishes_all_unpublished_events_and_marks_them():
    repo, publisher = _inject(events=[_event("evt-1", "user-1"), _event("evt-2", "user-2")])

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
        events=[_event("evt-1", "user-1"), _event("evt-2", "user-2")],
        fail_ids={"evt-1"},
    )

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 1, "failedCount": 1}
    assert repo.marked_published == ["evt-2"]  # evt-1 left unpublished for retry


def test_mark_failure_after_successful_publish_is_counted_failed_not_silently_dropped():
    repo, publisher = _inject(events=[_event("evt-1", "user-1")], mark_fail_ids={"evt-1"})

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 1}
    assert len(publisher.published) == 1  # it really was published
    assert repo.marked_published == []  # but never marked -> will retry


def test_unexpected_exception_for_one_event_does_not_kill_the_batch():
    repo, publisher = _inject(events=[_event("evt-1", "user-1"), _event("evt-2", "user-2")])

    def _raise(*args, **kwargs):
        raise KeyError("type")

    publisher.publish = _raise

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 2}


def test_get_unpublished_events_failure_returns_zero_counts_without_raising():
    class FailingRepository(FakeRepository):
        def get_unpublished_outbox_events(self, limit):
            raise CartServiceError("scan failed")

    outbox_publisher_handler._deps = {
        "repository": FailingRepository(),
        "publisher": FakePublisher(),
    }

    result = outbox_publisher_handler.handler({}, None)

    assert result == {"publishedCount": 0, "failedCount": 0}
