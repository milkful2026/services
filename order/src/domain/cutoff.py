"""The IST delivery cut-off, in one place so checkout (MA-136 FR-8), the
subscription-order sweep (MA-143 D-5) and checkout recovery (MA-144 PD-1)
can't drift apart."""

from datetime import date, datetime, time, timedelta, timezone

# Fixed offset, same as Subscription Service's own IST (India has no DST),
# so no dependency on the host's tz database.
IST = timezone(timedelta(hours=5, minutes=30))


def delivery_cutoff_passed(delivery_date: date, now: datetime, cutoff_hour_ist: int) -> bool:
    """True once `delivery_date` can no longer be scheduled: at or after
    `cutoff_hour_ist`:00 IST on the day before it."""
    cutoff = datetime.combine(delivery_date - timedelta(days=1), time(cutoff_hour_ist), IST)
    return now.astimezone(IST) >= cutoff
