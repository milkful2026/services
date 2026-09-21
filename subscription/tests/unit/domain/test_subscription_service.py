from datetime import date, datetime, time, timedelta

import pytest

from domain.exceptions import (
    CutoffPassedError,
    DateNotDueError,
    InvalidRangeError,
    InvalidScheduleError,
    ProductNotEligibleError,
    SubscriptionNotFoundError,
    SubscriptionStoppedError,
)
from domain.models import IST, PendingEdit, Schedule, ScheduleType, Subscription, SubscriptionStatus
from domain.subscription_service import is_due

TODAY = date(2026, 1, 15)
BEFORE_CUTOFF = datetime.combine(TODAY, time(10, 0), tzinfo=IST)  # cutoff default is 20:00 IST
AFTER_CUTOFF = datetime.combine(TODAY, time(21, 0), tzinfo=IST)


def _daily_schedule() -> Schedule:
    return Schedule(type=ScheduleType.DAILY)


def _make_sub(**overrides) -> Subscription:
    defaults = dict(
        id="sub_1",
        user_id="user-1",
        product_id="prod-1",
        quantity=1,
        schedule=_daily_schedule(),
        slot_id="slot-1",
        status=SubscriptionStatus.ACTIVE,
        start_date=TODAY,
    )
    defaults.update(overrides)
    return Subscription(**defaults)


class TestIsDue:
    def test_daily_always_due_after_start(self):
        sub = _make_sub(start_date=TODAY)
        assert is_due(sub, TODAY) is True
        assert is_due(sub, TODAY + timedelta(days=5)) is True

    def test_alternate_days_parity(self):
        sub = _make_sub(schedule=Schedule(type=ScheduleType.ALTERNATE_DAYS), start_date=TODAY)
        assert is_due(sub, TODAY) is True
        assert is_due(sub, TODAY + timedelta(days=1)) is False
        assert is_due(sub, TODAY + timedelta(days=2)) is True

    def test_weekly_matches_day_of_week(self):
        weekday = TODAY.isoweekday()
        sub = _make_sub(
            schedule=Schedule(type=ScheduleType.WEEKLY, days_of_week=[weekday]), start_date=TODAY
        )
        assert is_due(sub, TODAY) is True
        assert is_due(sub, TODAY + timedelta(days=1)) is False
        assert is_due(sub, TODAY + timedelta(days=7)) is True

    def test_custom_days_matches_any_listed_day(self):
        w1, w2 = TODAY.isoweekday(), (TODAY + timedelta(days=2)).isoweekday()
        sub = _make_sub(
            schedule=Schedule(type=ScheduleType.CUSTOM_DAYS, days_of_week=[w1, w2]),
            start_date=TODAY,
        )
        assert is_due(sub, TODAY) is True
        assert is_due(sub, TODAY + timedelta(days=1)) is False
        assert is_due(sub, TODAY + timedelta(days=2)) is True

    @pytest.mark.parametrize(
        "schedule",
        [
            Schedule(type=ScheduleType.DAILY),
            Schedule(type=ScheduleType.ALTERNATE_DAYS),
            Schedule(type=ScheduleType.WEEKLY, days_of_week=[TODAY.isoweekday()]),
            Schedule(type=ScheduleType.CUSTOM_DAYS, days_of_week=[TODAY.isoweekday()]),
        ],
    )
    def test_never_due_before_start_date_regardless_of_schedule_type(self, schedule):
        # Regression: the target_date < start_date guard must be
        # schedule-type-independent — including DAILY/WEEKLY cases whose
        # own due condition would otherwise be satisfied.
        sub = _make_sub(schedule=schedule, start_date=TODAY)
        assert is_due(sub, TODAY - timedelta(days=1)) is False
        assert is_due(sub, TODAY - timedelta(days=2)) is False

    def test_alternate_days_guard_not_inferred_from_parity_alone(self):
        # The specific regression this guard exists for: parity alone can
        # land on 0 for a date *before* start_date too (two full periods
        # early), so the explicit guard must run first.
        sub = _make_sub(schedule=Schedule(type=ScheduleType.ALTERNATE_DAYS), start_date=TODAY)
        two_periods_early = TODAY - timedelta(days=4)
        assert (two_periods_early - TODAY).days % 2 == 0  # parity alone would say "due"
        assert is_due(sub, two_periods_early) is False

    def test_stopped_never_due(self):
        sub = _make_sub(status=SubscriptionStatus.STOPPED, start_date=TODAY)
        assert is_due(sub, TODAY) is False

    def test_paused_window_excludes_dates_inside_it(self):
        sub = _make_sub(
            start_date=TODAY, pause_from=TODAY, pause_until=TODAY + timedelta(days=3)
        )
        assert is_due(sub, TODAY) is False
        assert is_due(sub, TODAY + timedelta(days=3)) is False
        assert is_due(sub, TODAY + timedelta(days=4)) is True

    def test_indefinite_pause_excludes_from_pause_from_onward(self):
        sub = _make_sub(start_date=TODAY, pause_from=TODAY, pause_until=None)
        assert is_due(sub, TODAY) is False
        assert is_due(sub, TODAY + timedelta(days=100)) is False

    def test_skipped_date_excluded(self):
        sub = _make_sub(start_date=TODAY)
        assert is_due(sub, TODAY, skipped_dates=frozenset({TODAY})) is False
        assert is_due(sub, TODAY) is True  # unaffected without the skip set


class TestCreate:
    def test_valid_daily_schedule(self, service):
        result = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=2,
            schedule=Schedule(type=ScheduleType.DAILY),
            start_date=TODAY + timedelta(days=1),
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id="corr-1",
            now=BEFORE_CUTOFF,
        )
        assert result["status"] == "ACTIVE"
        assert result["subscriptionId"].startswith("sub_")

    @pytest.mark.parametrize(
        "schedule",
        [
            Schedule(type=ScheduleType.WEEKLY, days_of_week=[]),  # empty -> invalid
            Schedule(type=ScheduleType.CUSTOM_DAYS, days_of_week=None),  # missing -> invalid
            Schedule(type=ScheduleType.DAILY, days_of_week=[1]),  # present when it shouldn't be
        ],
    )
    def test_invalid_schedule_rejected(self, service, schedule):
        with pytest.raises(InvalidScheduleError):
            service.create(
                user_id="user-1",
                product_id="prod-1",
                quantity=1,
                schedule=schedule,
                start_date=TODAY,
                slot_id="slot-1",
                idempotency_key="key-1",
                correlation_id=None,
                now=BEFORE_CUTOFF,
            )

    def test_non_eligible_product_rejected(self, service, catalog_client):
        catalog_client.seed("prod-bad", subscription_eligible=False)
        with pytest.raises(ProductNotEligibleError):
            service.create(
                user_id="user-1",
                product_id="prod-bad",
                quantity=1,
                schedule=_daily_schedule(),
                start_date=TODAY,
                slot_id="slot-1",
                idempotency_key="key-1",
                correlation_id=None,
                now=BEFORE_CUTOFF,
            )

    def test_unknown_product_rejected(self, service):
        with pytest.raises(ProductNotEligibleError):
            service.create(
                user_id="user-1",
                product_id="does-not-exist",
                quantity=1,
                schedule=_daily_schedule(),
                start_date=TODAY,
                slot_id="slot-1",
                idempotency_key="key-1",
                correlation_id=None,
                now=BEFORE_CUTOFF,
            )

    def test_idempotent_replay_returns_original(self, service):
        first = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=BEFORE_CUTOFF,
        )
        second = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=99,  # ignored on replay
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=BEFORE_CUTOFF,
        )
        assert first["subscriptionId"] == second["subscriptionId"]

    def test_start_date_in_past_rejected(self, service):
        with pytest.raises(InvalidScheduleError):
            service.create(
                user_id="user-1",
                product_id="prod-1",
                quantity=1,
                schedule=_daily_schedule(),
                start_date=TODAY - timedelta(days=1),
                slot_id="slot-1",
                idempotency_key="key-1",
                correlation_id=None,
                now=BEFORE_CUTOFF,
            )


class TestCreateSameDayEmission:
    def test_due_today_before_cutoff_emits_and_logs(self, service, repo):
        result = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id="corr-1",
            now=BEFORE_CUTOFF,
        )
        unpub = repo.fetch_unpublished()
        due_events = [e for e in unpub if e["event_type"] == "SubscriptionOrderDue"]
        assert len(due_events) == 1
        assert due_events[0]["payload"]["deliveryDate"] == TODAY.isoformat()
        assert repo.list_logged_subscription_ids(TODAY) == {result["subscriptionId"]}
        # nextDeliveryDate is projected from tomorrow, not today again.
        assert result["nextDeliveryDate"] == (TODAY + timedelta(days=1)).isoformat()

    def test_due_today_after_cutoff_does_not_emit(self, service, repo):
        result = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )
        assert repo.fetch_unpublished() == []
        assert result["nextDeliveryDate"] == (TODAY + timedelta(days=1)).isoformat()

    def test_not_due_today_does_not_emit(self, service, repo):
        not_today_weekday = (TODAY + timedelta(days=1)).isoweekday()
        service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=Schedule(type=ScheduleType.WEEKLY, days_of_week=[not_today_weekday]),
            start_date=TODAY,  # today itself isn't a scheduled weekday
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=BEFORE_CUTOFF,
        )
        assert repo.fetch_unpublished() == []

    def test_retried_create_never_double_emits(self, service, repo):
        for _ in range(2):
            service.create(
                user_id="user-1",
                product_id="prod-1",
                quantity=1,
                schedule=_daily_schedule(),
                start_date=TODAY,
                slot_id="slot-1",
                idempotency_key="key-1",
                correlation_id=None,
                now=BEFORE_CUTOFF,
            )
        due_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "SubscriptionOrderDue"
        ]
        assert len(due_events) == 1

    def test_already_materialized_date_never_reported_as_next_delivery(self, service):
        # Regression (caught in CI, not locally): a DAILY subscription due
        # today gets same-day-emitted at create time, logging TODAY into
        # subscription_run_log. A later read (get/list/pause/resume/stop)
        # must not re-report TODAY as nextDeliveryDate just because
        # is_due(sub, TODAY) is still (correctly) true — it was already
        # turned into a SubscriptionOrderDue and must roll to tomorrow.
        created = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=BEFORE_CUTOFF,
        )
        sub_id = created["subscriptionId"]
        detail = service.get(sub_id, "user-1", now=BEFORE_CUTOFF)
        assert detail["nextDeliveryDate"] == (TODAY + timedelta(days=1)).isoformat()


class TestPauseResumeStop:
    def _create(self, service, **overrides):
        kwargs = dict(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,  # avoid same-day emission noise in these tests
        )
        kwargs.update(overrides)
        return service.create(**kwargs)["subscriptionId"]

    def test_pause_no_args_is_immediate_open_ended(self, service):
        sub_id = self._create(service)
        result = service.pause(sub_id, "user-1", from_=None, until=None, now=BEFORE_CUTOFF)
        assert result["pauseFrom"] == TODAY.isoformat()
        assert result["pauseUntil"] is None
        assert result["status"] == "PAUSED"

    def test_pause_until_only(self, service):
        sub_id = self._create(service)
        until = TODAY + timedelta(days=5)
        result = service.pause(sub_id, "user-1", from_=None, until=until, now=BEFORE_CUTOFF)
        assert result["pauseFrom"] == TODAY.isoformat()
        assert result["pauseUntil"] == until.isoformat()

    def test_pause_both_given(self, service):
        sub_id = self._create(service)
        frm, until = TODAY + timedelta(days=2), TODAY + timedelta(days=5)
        result = service.pause(sub_id, "user-1", from_=frm, until=until, now=BEFORE_CUTOFF)
        assert result["pauseFrom"] == frm.isoformat()
        assert result["pauseUntil"] == until.isoformat()

    def test_pause_past_from_rejected(self, service):
        sub_id = self._create(service)
        with pytest.raises(InvalidRangeError):
            service.pause(
                sub_id, "user-1", from_=TODAY - timedelta(days=1), until=None, now=BEFORE_CUTOFF
            )

    def test_pause_until_before_from_rejected(self, service):
        sub_id = self._create(service)
        with pytest.raises(InvalidRangeError):
            service.pause(
                sub_id,
                "user-1",
                from_=TODAY + timedelta(days=5),
                until=TODAY + timedelta(days=1),
                now=BEFORE_CUTOFF,
            )

    def test_overlapping_pause_replaces_window(self, service):
        sub_id = self._create(service)
        service.pause(
            sub_id, "user-1", from_=None, until=TODAY + timedelta(days=10), now=BEFORE_CUTOFF
        )
        result = service.pause(
            sub_id, "user-1", from_=None, until=TODAY + timedelta(days=2), now=BEFORE_CUTOFF
        )
        assert result["pauseUntil"] == (TODAY + timedelta(days=2)).isoformat()

    def test_resume_from_paused(self, service):
        sub_id = self._create(service)
        service.pause(sub_id, "user-1", from_=None, until=None, now=BEFORE_CUTOFF)
        result = service.resume(sub_id, "user-1")
        assert result["status"] == "ACTIVE"
        assert result["pauseFrom"] is None

    def test_resume_from_scheduled_future_pause(self, service):
        sub_id = self._create(service)
        service.pause(
            sub_id, "user-1", from_=TODAY + timedelta(days=5), until=None, now=BEFORE_CUTOFF
        )
        result = service.resume(sub_id, "user-1")
        assert result["pauseFrom"] is None

    def test_resume_rejected_from_stopped(self, service):
        sub_id = self._create(service)
        service.stop(sub_id, "user-1")
        with pytest.raises(SubscriptionStoppedError):
            service.resume(sub_id, "user-1")

    def test_stop_is_idempotent(self, service):
        sub_id = self._create(service)
        first = service.stop(sub_id, "user-1")
        second = service.stop(sub_id, "user-1")
        assert first["status"] == second["status"] == "STOPPED"

    def test_operating_on_someone_elses_subscription_is_not_found(self, service):
        sub_id = self._create(service)
        with pytest.raises(SubscriptionNotFoundError):
            service.pause(sub_id, "some-other-user", from_=None, until=None, now=BEFORE_CUTOFF)


class TestSkip:
    def _create_daily(self, service):
        return service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]

    def test_skip_valid_future_due_date(self, service, repo):
        sub_id = self._create_daily(service)
        target = TODAY + timedelta(days=5)
        service.skip(sub_id, "user-1", target, now=BEFORE_CUTOFF)
        assert target in repo.list_skip_dates(sub_id)

    def test_skip_non_due_date_refused(self, service):
        sub_id = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=Schedule(type=ScheduleType.ALTERNATE_DAYS),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]
        with pytest.raises(DateNotDueError):
            service.skip(sub_id, "user-1", TODAY + timedelta(days=1), now=BEFORE_CUTOFF)

    def test_skip_after_cutoff_refused(self, service):
        sub_id = self._create_daily(service)
        tomorrow = TODAY + timedelta(days=1)
        # Cutoff for `tomorrow` is today's cutoff hour (20:00 IST) — past it.
        with pytest.raises(CutoffPassedError):
            service.skip(sub_id, "user-1", tomorrow, now=AFTER_CUTOFF)


class TestEdit:
    def _create_daily(self, service):
        return service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]

    def test_before_cutoff_applies_immediately(self, service, repo):
        sub_id = self._create_daily(service)
        result = service.edit(sub_id, "user-1", quantity=5, schedule=None, now=BEFORE_CUTOFF)
        assert result["effectiveFrom"] == (TODAY + timedelta(days=1)).isoformat()
        updated = repo.get_by_id(sub_id)
        assert updated.quantity == 5
        assert updated.pending_edit is None

    def test_after_cutoff_defers_to_pending_edit(self, service, repo):
        sub_id = self._create_daily(service)
        result = service.edit(sub_id, "user-1", quantity=5, schedule=None, now=AFTER_CUTOFF)
        expected_effective = TODAY + timedelta(days=2)  # one cycle further out
        assert result["effectiveFrom"] == expected_effective.isoformat()
        updated = repo.get_by_id(sub_id)
        assert updated.quantity == 1  # unchanged yet
        assert updated.pending_edit == PendingEdit(
            quantity=5, schedule=_daily_schedule(), effective_from=expected_effective
        )

    def test_invalid_schedule_on_edit_rejected(self, service):
        sub_id = self._create_daily(service)
        with pytest.raises(InvalidScheduleError):
            service.edit(
                sub_id,
                "user-1",
                quantity=None,
                schedule=Schedule(type=ScheduleType.WEEKLY, days_of_week=[]),
                now=BEFORE_CUTOFF,
            )


class TestRunDaily:
    def test_daily_subscription_emits_for_tomorrow(self, service, repo):
        service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,  # skip same-day emission so run_daily is the only source
        )
        due_ids = service.run_daily(now=AFTER_CUTOFF)
        assert len(due_ids) == 1
        tomorrow = TODAY + timedelta(days=1)
        assert repo.list_logged_subscription_ids(tomorrow) == set(due_ids)

    def test_paused_subscription_excluded(self, service):
        sub_id = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]
        service.pause(sub_id, "user-1", from_=None, until=None, now=BEFORE_CUTOFF)
        due_ids = service.run_daily(now=AFTER_CUTOFF)
        assert due_ids == []

    def test_skipped_date_excluded(self, service):
        sub_id = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]
        tomorrow = TODAY + timedelta(days=1)
        service.skip(sub_id, "user-1", tomorrow, now=BEFORE_CUTOFF)
        due_ids = service.run_daily(now=AFTER_CUTOFF)
        assert due_ids == []

    def test_pending_edit_applied_before_due_check(self, service, repo):
        sub_id = service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )["subscriptionId"]
        tomorrow = TODAY + timedelta(days=1)
        service.edit(sub_id, "user-1", quantity=7, schedule=None, now=AFTER_CUTOFF)
        # After-cutoff edit defers to tomorrow+1; force the pending edit's
        # effective_from to land on tomorrow for this test by editing the
        # stored row directly via the repo (simulating "the day arrived").
        repo.set_pending_edit(
            sub_id, PendingEdit(quantity=7, schedule=_daily_schedule(), effective_from=tomorrow)
        )
        service.run_daily(now=AFTER_CUTOFF)
        updated = repo.get_by_id(sub_id)
        assert updated.quantity == 7
        assert updated.pending_edit is None

    def test_duplicate_run_for_same_cutoff_emits_nothing_new(self, service, repo):
        service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )
        first = service.run_daily(now=AFTER_CUTOFF)
        second = service.run_daily(now=AFTER_CUTOFF)
        assert len(first) == 1
        assert second == []
        due_events = [
            e for e in repo.fetch_unpublished() if e["event_type"] == "SubscriptionOrderDue"
        ]
        assert len(due_events) == 1

    def test_one_bad_subscription_does_not_abort_the_run(self, service, repo, monkeypatch):
        service.create(
            user_id="user-1",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-1",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )
        service.create(
            user_id="user-2",
            product_id="prod-1",
            quantity=1,
            schedule=_daily_schedule(),
            start_date=TODAY,
            slot_id="slot-1",
            idempotency_key="key-2",
            correlation_id=None,
            now=AFTER_CUTOFF,
        )

        real_list_skip_dates = repo.list_skip_dates
        call_count = {"n": 0}

        def _flaky_list_skip_dates(subscription_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure for the first subscription only")
            return real_list_skip_dates(subscription_id)

        monkeypatch.setattr(repo, "list_skip_dates", _flaky_list_skip_dates)
        due_ids = service.run_daily(now=AFTER_CUTOFF)
        assert len(due_ids) == 1  # the second subscription still got processed
