"""Ports the domain depends on. Adapters implement these; the domain
never imports SQLAlchemy or `requests` directly."""

from datetime import date
from typing import Protocol

from domain.models import PendingEdit, Schedule, Subscription, SubscriptionStatus


class SubscriptionRepositoryPort(Protocol):
    def insert_if_absent(
        self,
        *,
        subscription: Subscription,
        idempotency_key: str,
        same_day_delivery_date: date | None,
        outbox_event_type: str | None,
        outbox_payload: dict | None,
    ) -> tuple[Subscription, bool]:
        """One transaction: insert the subscription row keyed on
        `(user_id, idempotency_key)`; if `same_day_delivery_date` is not
        None, also insert the `subscription_run_log` row and the outbox
        row for the same-day `SubscriptionOrderDue`, atomically. Returns
        `(subscription, True)` on a fresh insert. A concurrent duplicate
        insert (unique-constraint race — the caller already checked
        `get_by_idempotency_key` first, so this is only the narrow race
        window) returns `(existing_subscription, False)` instead of
        raising."""
        ...

    def get_by_id(self, subscription_id: str) -> Subscription | None: ...

    def get_by_idempotency_key(self, user_id: str, idempotency_key: str) -> Subscription | None: ...

    def list_by_user(self, user_id: str) -> list[Subscription]: ...

    def list_active(self) -> list[Subscription]:
        """Daily Run candidates: every non-STOPPED subscription (ACTIVE or
        PAUSED) — a future-dated pause is already PAUSED at the status
        level ahead of `pause_from` arriving, so a stricter `status ==
        ACTIVE` filter would wrongly drop it from the run before the
        pause actually starts."""
        ...

    def update_status(self, subscription_id: str, status: SubscriptionStatus) -> Subscription: ...

    def update_pause(
        self,
        subscription_id: str,
        *,
        pause_from: date | None,
        pause_until: date | None,
        status: SubscriptionStatus,
    ) -> Subscription: ...

    def apply_edit_now(
        self, subscription_id: str, quantity: int, schedule: Schedule
    ) -> Subscription:
        """Applies quantity/schedule immediately and clears any pending edit."""
        ...

    def set_pending_edit(self, subscription_id: str, pending_edit: PendingEdit) -> Subscription: ...

    def apply_pending_edit(
        self, subscription_id: str, quantity: int, schedule: Schedule
    ) -> Subscription:
        """Same as `apply_edit_now`, called by the Daily Run once a
        pending edit's `effective_from` has arrived."""
        ...

    def insert_skip(self, subscription_id: str, skip_date: date) -> None: ...

    def list_skip_dates(self, subscription_id: str) -> set[date]: ...

    def list_skip_dates_batch(self, subscription_ids: list[str]) -> dict[str, set[date]]:
        """Batched — one query for every subscription's skip dates, keyed
        by subscription_id (missing/no-skip ids map to an empty set), so
        `run_daily` never queries skip dates per subscription."""
        ...

    def list_logged_dates(self, subscription_id: str) -> set[date]:
        """Every `delivery_date` already recorded in `subscription_run_log`
        for this subscription — used to keep `nextDeliveryDate` projection
        from re-reporting an already-materialized date as upcoming."""
        ...

    def list_logged_subscription_ids(self, delivery_date: date) -> set[str]:
        """Batched — one query for every subscription already logged for
        `delivery_date`, so `run_daily` never queries per subscription."""
        ...

    def insert_run_log_and_enqueue(
        self,
        *,
        subscription_id: str,
        delivery_date: date,
        outbox_event_type: str,
        outbox_payload: dict,
    ) -> bool:
        """One transaction: insert `(subscription_id, delivery_date)` into
        `subscription_run_log` (UNIQUE) and an outbox row for the event.
        Returns True if newly inserted, False if the run_log row already
        existed (duplicate Scheduler invocation — no second outbox row)."""
        ...


class CatalogClientPort(Protocol):
    def get_product(self, product_id: str) -> dict | None:
        """`GET /products/{id}`. Returns the product's `data` dict (with
        `subscriptionEligible`), or None if Catalog reports 404. Raises
        `CatalogUnavailableError` after retries are exhausted — never
        silently guesses eligibility."""
        ...
