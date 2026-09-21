"""Subscription domain service — the only place business rules live.

Covers MA-131: lifecycle (create/pause/resume/stop/skip/edit), the
due-computation function shared by create's same-day case, the Daily
Run, and next-delivery-date projection, and the Daily Run itself.

Known, deliberate scope trim vs. MA-131 FR-2's full text: a future-dated
pause (`from` in the future) stores `status = PAUSED` immediately rather
than keeping `status = ACTIVE` with a separate `scheduledPauseFrom` field
until `from` arrives. `is_due`/the Daily Run still correctly exclude only
the actual `[pause_from, pause_until]` window regardless of the stored
status, so due-date computation is unaffected — this trim only changes
what the coarse `status` field reads as for the few days before a
future-dated pause actually starts.
"""

import logging
import uuid
from datetime import date, datetime, time, timedelta

from adapters.interfaces import CatalogClientPort, SubscriptionRepositoryPort
from config.env import Settings
from domain.exceptions import (
    CutoffPassedError,
    DateNotDueError,
    InvalidRangeError,
    InvalidScheduleError,
    ProductNotEligibleError,
    SubscriptionNotFoundError,
    SubscriptionStoppedError,
)
from domain.models import (
    IST,
    PendingEdit,
    Schedule,
    ScheduleType,
    Subscription,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)

_PROJECTION_HORIZON_DAYS = 90


def is_due(
    subscription: Subscription, target_date: date, skipped_dates: frozenset[date] = frozenset()
) -> bool:
    """MA-131 FR-8's due-computation function — pure, no I/O, callable for
    any target date (the Daily Run always uses tomorrow; `create`'s
    same-day case and `nextDeliveryDate` projection use others).

    `target_date < start_date` is checked unconditionally, first, before
    any type-specific rule — it is NOT implied by `ALTERNATE_DAYS`'s own
    parity formula (which is periodic and can land on 0 for a date before
    `start_date` too, e.g. two full periods early)."""
    if target_date < subscription.start_date:
        return False
    if subscription.status == SubscriptionStatus.STOPPED:
        return False
    if _in_pause_window(subscription, target_date):
        return False
    if target_date in skipped_dates:
        return False

    schedule = subscription.schedule
    if schedule.type == ScheduleType.DAILY:
        return True
    if schedule.type == ScheduleType.ALTERNATE_DAYS:
        return (target_date - subscription.start_date).days % 2 == 0
    # WEEKLY / CUSTOM_DAYS
    return target_date.isoweekday() in (schedule.days_of_week or [])


def _in_pause_window(subscription: Subscription, target_date: date) -> bool:
    if subscription.pause_from is None:
        return False
    if subscription.pause_until is None:
        return target_date >= subscription.pause_from
    return subscription.pause_from <= target_date <= subscription.pause_until


def _validate_schedule(schedule: Schedule) -> None:
    needs_days = schedule.type in (ScheduleType.WEEKLY, ScheduleType.CUSTOM_DAYS)
    has_days = bool(schedule.days_of_week)
    if needs_days and not has_days:
        raise InvalidScheduleError(
            f"daysOfWeek is required and non-empty for {schedule.type.value}"
        )
    if not needs_days and schedule.days_of_week:
        raise InvalidScheduleError(f"daysOfWeek must be absent for {schedule.type.value}")
    if has_days and any(d < 1 or d > 7 for d in schedule.days_of_week):
        raise InvalidScheduleError("daysOfWeek values must be ISO weekdays 1-7")


def _cutoff_moment(target_date: date, cutoff_hour: int) -> datetime:
    """The evening-before deadline for `target_date`: `cutoff_hour` IST on
    `target_date - 1 day`. Used uniformly for the Daily Run (tomorrow's
    cutoff = today's cutoff_hour), skip, and edit."""
    return datetime.combine(target_date - timedelta(days=1), time(cutoff_hour), tzinfo=IST)


def _todays_cutoff_moment(now: datetime, cutoff_hour: int) -> datetime:
    """`create`'s same-day special case only: today's own cutoff_hour, not
    the day-before-target formula above — there is no "day before" for a
    subscription whose first possible delivery is today itself."""
    return datetime.combine(now.astimezone(IST).date(), time(cutoff_hour), tzinfo=IST)


def _next_delivery_date(
    subscription: Subscription,
    skipped_dates: frozenset[date],
    start_from: date,
    logged_dates: frozenset[date] = frozenset(),
    horizon_days: int = _PROJECTION_HORIZON_DAYS,
) -> date | None:
    """Projects the soonest due, non-skipped, non-already-materialized
    date from `start_from` onward. `logged_dates` (from
    `subscription_run_log`) is distinct from `skipped_dates`: a date
    already recorded there was already turned into a `SubscriptionOrderDue`
    (via create's same-day case or a prior Daily Run) and must never be
    re-reported as still-upcoming, even though `is_due` alone would still
    say yes for it."""
    if subscription.status == SubscriptionStatus.STOPPED:
        return None
    if subscription.pause_from is not None and subscription.pause_until is None:
        return None  # indefinite pause, no resume date to project past
    for offset in range(horizon_days):
        candidate = start_from + timedelta(days=offset)
        if candidate in logged_dates:
            continue
        if is_due(subscription, candidate, skipped_dates):
            return candidate
    return None


def new_subscription_id() -> str:
    return f"sub_{uuid.uuid4().hex}"


class SubscriptionService:
    def __init__(
        self,
        repository: SubscriptionRepositoryPort,
        catalog_client: CatalogClientPort,
        settings: Settings,
    ) -> None:
        self._repo = repository
        self._catalog = catalog_client
        self._settings = settings

    def _get_owned(self, subscription_id: str, user_id: str) -> Subscription:
        sub = self._repo.get_by_id(subscription_id)
        if sub is None or sub.user_id != user_id:
            # 404, not 403 — don't leak existence to a non-owner (same
            # posture as Payment Service's own owner-scoped reads).
            raise SubscriptionNotFoundError(f"No subscription {subscription_id!r}")
        return sub

    # --- FR-1: create ---

    def create(
        self,
        *,
        user_id: str,
        product_id: str,
        quantity: int,
        schedule: Schedule,
        start_date: date,
        slot_id: str,
        idempotency_key: str,
        correlation_id: str | None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(IST)
        today = now.astimezone(IST).date()

        existing = self._repo.get_by_idempotency_key(user_id, idempotency_key)
        if existing is not None:
            return self._create_response(existing, now)

        if start_date < today:
            raise InvalidScheduleError("startDate must be today or later")
        _validate_schedule(schedule)

        product = self._catalog.get_product(product_id)
        if product is None or not product.get("subscriptionEligible", False):
            raise ProductNotEligibleError(f"Product {product_id!r} is not subscription-eligible")

        subscription = Subscription(
            id=new_subscription_id(),
            user_id=user_id,
            product_id=product_id,
            quantity=quantity,
            schedule=schedule,
            slot_id=slot_id,
            status=SubscriptionStatus.ACTIVE,
            start_date=start_date,
        )

        same_day_due = (
            start_date == today
            and is_due(subscription, today)
            and now < _todays_cutoff_moment(now, self._settings.cutoff_hour_ist)
        )

        outbox_payload = None
        if same_day_due:
            outbox_payload = {
                "eventId": str(uuid.uuid4()),
                "occurredAt": now.isoformat(),
                "subscriptionId": subscription.id,
                "userId": user_id,
                "productId": product_id,
                "quantity": quantity,
                "deliveryDate": today.isoformat(),
                "slotId": slot_id,
                "correlationId": correlation_id or str(uuid.uuid4()),
            }

        result, created = self._repo.insert_if_absent(
            subscription=subscription,
            idempotency_key=idempotency_key,
            same_day_delivery_date=today if same_day_due else None,
            outbox_event_type="SubscriptionOrderDue" if same_day_due else None,
            outbox_payload=outbox_payload,
        )
        logger.info(
            "subscription.created" if created else "subscription.create_replay",
            extra={"subscriptionId": result.id, "userId": user_id},
        )
        return self._create_response(result, now)

    def _project_next_delivery(self, subscription: Subscription, now: datetime) -> date | None:
        """Today is a valid candidate only while it's still before today's
        own cut-off (matching create's same-day rule); past it, today is
        no longer reachable regardless of what the schedule says (MA-131
        §9's own edge case). Already-materialized dates (`subscription_run_log`)
        are excluded so a just-emitted same-day/Daily-Run date is never
        re-reported as still-upcoming."""
        today = now.astimezone(IST).date()
        start_from = (
            today if now < _todays_cutoff_moment(now, self._settings.cutoff_hour_ist)
            else today + timedelta(days=1)
        )
        skipped = frozenset(self._repo.list_skip_dates(subscription.id))
        logged = frozenset(self._repo.list_logged_dates(subscription.id))
        return _next_delivery_date(subscription, skipped, start_from, logged)

    def _create_response(self, subscription: Subscription, now: datetime) -> dict:
        next_delivery = self._project_next_delivery(subscription, now)
        return {
            "subscriptionId": subscription.id,
            "status": subscription.status.value,
            "nextDeliveryDate": next_delivery.isoformat() if next_delivery else None,
        }

    # --- FR-2/FR-3/FR-4: pause / resume / stop ---

    def pause(
        self,
        subscription_id: str,
        user_id: str,
        *,
        from_: date | None,
        until: date | None,
        now: datetime | None = None,
    ) -> dict:
        sub = self._get_owned(subscription_id, user_id)
        if sub.status == SubscriptionStatus.STOPPED:
            raise SubscriptionStoppedError("Cannot pause a stopped subscription")

        now = now or datetime.now(IST)
        today = now.astimezone(IST).date()
        effective_from = from_ or today
        if effective_from < today:
            raise InvalidRangeError("'from' cannot be in the past")
        if until is not None and until < effective_from:
            raise InvalidRangeError("'until' cannot be before 'from'")

        updated = self._repo.update_pause(
            sub.id,
            pause_from=effective_from,
            pause_until=until,
            status=SubscriptionStatus.PAUSED,
        )
        return self._detail_response(updated, now)

    def resume(self, subscription_id: str, user_id: str, *, now: datetime | None = None) -> dict:
        sub = self._get_owned(subscription_id, user_id)
        if sub.status == SubscriptionStatus.STOPPED:
            raise SubscriptionStoppedError("Cannot resume a stopped subscription")
        updated = self._repo.update_pause(
            sub.id, pause_from=None, pause_until=None, status=SubscriptionStatus.ACTIVE
        )
        return self._detail_response(updated, now)

    def stop(self, subscription_id: str, user_id: str, *, now: datetime | None = None) -> dict:
        sub = self._get_owned(subscription_id, user_id)
        if sub.status != SubscriptionStatus.STOPPED:
            sub = self._repo.update_status(sub.id, SubscriptionStatus.STOPPED)
        return self._detail_response(sub, now)  # idempotent — same response either way

    # --- FR-5: skip ---

    def skip(
        self, subscription_id: str, user_id: str, skip_date: date, *, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(IST)
        sub = self._get_owned(subscription_id, user_id)
        skipped = frozenset(self._repo.list_skip_dates(sub.id))
        if not is_due(sub, skip_date, skipped):
            raise DateNotDueError(f"{skip_date.isoformat()} is not a due date for this schedule")
        if now >= _cutoff_moment(skip_date, self._settings.cutoff_hour_ist):
            raise CutoffPassedError(f"Cut-off has passed for {skip_date.isoformat()}")
        self._repo.insert_skip(sub.id, skip_date)

    # --- FR-6: edit ---

    def edit(
        self,
        subscription_id: str,
        user_id: str,
        *,
        quantity: int | None,
        schedule: Schedule | None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(IST)
        sub = self._get_owned(subscription_id, user_id)
        new_quantity = quantity if quantity is not None else sub.quantity
        new_schedule = schedule if schedule is not None else sub.schedule
        _validate_schedule(new_schedule)

        skipped = frozenset(self._repo.list_skip_dates(sub.id))
        logged = frozenset(self._repo.list_logged_dates(sub.id))
        tomorrow = now.astimezone(IST).date() + timedelta(days=1)
        next_due = _next_delivery_date(sub, skipped, tomorrow, logged)

        if next_due is None or now < _cutoff_moment(next_due, self._settings.cutoff_hour_ist):
            self._repo.apply_edit_now(sub.id, new_quantity, new_schedule)
            return {"effectiveFrom": next_due.isoformat() if next_due else None}

        after_next = _next_delivery_date(
            sub, skipped, next_due + timedelta(days=1), logged
        ) or (next_due + timedelta(days=1))
        self._repo.set_pending_edit(
            sub.id,
            PendingEdit(quantity=new_quantity, schedule=new_schedule, effective_from=after_next),
        )
        return {"effectiveFrom": after_next.isoformat()}

    # --- FR-9: read APIs ---

    def get(self, subscription_id: str, user_id: str, *, now: datetime | None = None) -> dict:
        sub = self._get_owned(subscription_id, user_id)
        return self._detail_response(sub, now)

    def list_for_user(self, user_id: str, *, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(IST)
        return [self._detail_response(s, now) for s in self._repo.list_by_user(user_id)]

    def _detail_response(self, sub: Subscription, now: datetime | None = None) -> dict:
        now = now or datetime.now(IST)
        skipped = self._repo.list_skip_dates(sub.id)
        next_delivery = self._project_next_delivery(sub, now)
        return {
            "subscriptionId": sub.id,
            "productId": sub.product_id,
            "quantity": sub.quantity,
            "schedule": sub.schedule.to_dict(),
            "slotId": sub.slot_id,
            "status": sub.status.value,
            "startDate": sub.start_date.isoformat(),
            "pauseFrom": sub.pause_from.isoformat() if sub.pause_from else None,
            "pauseUntil": sub.pause_until.isoformat() if sub.pause_until else None,
            "pendingEdit": sub.pending_edit.to_dict() if sub.pending_edit else None,
            "nextDeliveryDate": next_delivery.isoformat() if next_delivery else None,
            "skippedDates": sorted(d.isoformat() for d in skipped),
        }

    # --- FR-8: Daily Run ---

    def run_daily(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(IST)
        tomorrow = now.astimezone(IST).date() + timedelta(days=1)
        # One correlation id for the whole run — there is no inbound
        # request to inherit one from (unlike `create`), and every event
        # schema requires a non-empty correlationId; this also lets every
        # order this run produced be traced back to "Daily Run at <time>".
        run_correlation_id = str(uuid.uuid4())

        already_logged = self._repo.list_logged_subscription_ids(tomorrow)
        due_subscription_ids: list[str] = []

        for sub in self._repo.list_active():
            try:
                if sub.id in already_logged:
                    continue
                if sub.pending_edit is not None and sub.pending_edit.effective_from <= tomorrow:
                    sub = self._repo.apply_pending_edit(
                        sub.id, sub.pending_edit.quantity, sub.pending_edit.schedule
                    )
                skipped = frozenset(self._repo.list_skip_dates(sub.id))
                if not is_due(sub, tomorrow, skipped):
                    continue
                emitted = self._repo.insert_run_log_and_enqueue(
                    subscription_id=sub.id,
                    delivery_date=tomorrow,
                    outbox_event_type="SubscriptionOrderDue",
                    outbox_payload={
                        "eventId": str(uuid.uuid4()),
                        "occurredAt": now.isoformat(),
                        "subscriptionId": sub.id,
                        "userId": sub.user_id,
                        "productId": sub.product_id,
                        "quantity": sub.quantity,
                        "deliveryDate": tomorrow.isoformat(),
                        "slotId": sub.slot_id,
                        "correlationId": run_correlation_id,
                    },
                )
                if emitted:
                    due_subscription_ids.append(sub.id)
            except Exception:  # noqa: BLE001 — one bad row must not abort the run (MA-131 §5 NFR)
                logger.exception(
                    "run_daily: subscription failed, continuing",
                    extra={"subscriptionId": sub.id},
                )

        logger.info(
            "subscription.daily_run_due_count",
            extra={
                "metric": "subscription.daily_run_due_count",
                "count": len(due_subscription_ids),
            },
        )
        return due_subscription_ids
