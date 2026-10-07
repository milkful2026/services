"""The IST day-before deadlines, in one place so checkout (MA-136 FR-8),
checkout recovery (MA-144 PD-1), the subscription-order sweep's charge
deadline (MA-143 D-5, a later hour than the cut-off) and a customer cancel
(MA-154: the check and the `cancellableUntil` it reports) can't drift apart."""

from datetime import date, datetime, time, timedelta, timezone

# Fixed offset, same as Subscription Service's own IST (India has no DST),
# so no dependency on the host's tz database.
IST = timezone(timedelta(hours=5, minutes=30))


def delivery_cutoff_moment(delivery_date: date, cutoff_hour_ist: int) -> datetime:
    """`cutoff_hour_ist`:00 IST on the day before `delivery_date`, aware."""
    return datetime.combine(delivery_date - timedelta(days=1), time(cutoff_hour_ist), IST)


def delivery_cutoff_passed(delivery_date: date, now: datetime, cutoff_hour_ist: int) -> bool:
    """True at or after `cutoff_hour_ist`:00 IST on the day before
    `delivery_date` — the checkout cut-off, or (with the charge-deadline
    hour) the last moment a subscription order may still be charged."""
    return now.astimezone(IST) >= delivery_cutoff_moment(delivery_date, cutoff_hour_ist)
