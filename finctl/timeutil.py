"""Time handling for Indian settlement cycles.

Two failure modes this module exists to prevent:

1. Timezone drift. A payment captured at 2026-07-15T23:45:00Z happened on
   2026-07-16 in IST. Reconciling on the UTC date puts it in the wrong day's
   batch and it never matches. Every timestamp is normalised to IST before a
   business date is derived from it.

2. Naive T+N windows. "T+2" means two *banking* days, not two calendar days.
   A Friday capture settles Tuesday, and a settlement falling on Diwali slips
   further. Matching on a calendar window silently misses these.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Final, Iterable

# India Standard Time. Fixed offset, no DST, which makes this exact.
IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30), name="IST")

# RBI-observed bank holidays. A real deployment would pull these from a feed;
# hardcoding the demo period keeps the system dependency-free while still
# exercising the holiday-slip logic that naive implementations get wrong.
BANK_HOLIDAYS: Final[frozenset[date]] = frozenset({
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 4),    # Holi
    date(2026, 3, 21),   # Id-ul-Fitr
    date(2026, 4, 1),    # Annual bank closing
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 6, 26),   # Bakrid
    date(2026, 8, 15),   # Independence Day
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 8),   # Diwali
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
})

# Formats seen across PSP exports, bank statements and ERP dumps. Ordered so
# that unambiguous ISO forms win before the ambiguous day/month ones.
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%b-%y",
    "%b %d, %Y",
    "%m/%d/%Y",
)


class DateParseError(ValueError):
    """Raised when a value cannot be resolved to an unambiguous date."""


def is_banking_day(d: date) -> bool:
    """True when Indian banks settle on this date.

    Saturday and Sunday are closed for settlement purposes. (Indian banks open
    on 1st/3rd Saturdays for branch business, but NEFT/RTGS settlement batches
    that reconciliation depends on do not run, so weekends are uniformly out.)
    """
    return d.weekday() < 5 and d not in BANK_HOLIDAYS


def next_banking_day(d: date) -> date:
    """The first banking day strictly after `d`."""
    nxt = d + timedelta(days=1)
    while not is_banking_day(nxt):
        nxt += timedelta(days=1)
    return nxt


@lru_cache(maxsize=4096)
def add_banking_days(start: date, n: int) -> date:
    """Advance `n` banking days from `start`, skipping weekends and holidays.

    n=0 returns `start` rolled forward to the next banking day if it is not one
    already, which is the behaviour a settlement cycle actually has.
    """
    current = start
    if n <= 0:
        while not is_banking_day(current):
            current += timedelta(days=1)
        return current
    for _ in range(n):
        current = next_banking_day(current)
    return current


def banking_days_between(a: date, b: date) -> int:
    """Count banking days from `a` to `b`. Negative when `b` precedes `a`.

    Used to express match tolerance in the unit that settlement actually moves
    in, so a Friday-to-Tuesday gap reads as 2, not 4.
    """
    if b < a:
        return -banking_days_between(b, a)
    count = 0
    current = a
    while current < b:
        current += timedelta(days=1)
        if is_banking_day(current):
            count += 1
    return count


def parse_datetime(raw: object, *, field: str = "timestamp") -> datetime:
    """Parse a timestamp from any of the formats these feeds use, into IST.

    Naive values are assumed to already be IST, which is the convention every
    Indian bank statement follows. Values carrying an explicit offset (a PSP
    API returning UTC, say) are converted, which is what fixes the midnight
    boundary bug.
    """
    if raw is None:
        raise DateParseError(f"{field}: value is missing")

    if isinstance(raw, datetime):
        return raw.astimezone(IST) if raw.tzinfo else raw.replace(tzinfo=IST)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=IST)

    # Unix epoch seconds, which PSP webhooks emit. Bounded to a sane range so a
    # stray order id is never silently read as a 1974 timestamp.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if 946_684_800 <= raw <= 4_102_444_800:  # 2000-01-01 .. 2100-01-01
            return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(IST)
        raise DateParseError(f"{field}: {raw!r} is out of plausible epoch range")

    s = str(raw).strip()
    if not s:
        raise DateParseError(f"{field}: value is empty")

    # Normalise the Zulu suffix, which strptime's %z does not accept directly.
    normalised = s[:-1] + "+0000" if s.endswith("Z") else s
    # Tolerate the ISO colon-in-offset form (+05:30) for the same reason.
    if len(normalised) >= 6 and normalised[-3] == ":" and normalised[-6] in "+-":
        normalised = normalised[:-3] + normalised[-2:]

    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(normalised, fmt)
        except ValueError:
            continue
        return parsed.astimezone(IST) if parsed.tzinfo else parsed.replace(tzinfo=IST)

    raise DateParseError(f"{field}: unrecognised date format {raw!r}")


def business_date(raw: object, *, field: str = "timestamp") -> date:
    """The IST calendar date a timestamp belongs to.

    This is the function that must be used anywhere a record is bucketed into a
    day. Calling `.date()` on a UTC timestamp instead is the timezone bug.
    """
    return parse_datetime(raw, field=field).date()


def settlement_window(captured: date, *, cycle_days: int = 2, slack: int = 1) -> tuple[date, date]:
    """The banking-day range a capture is expected to land in the bank on.

    `cycle_days` is the contracted T+N. `slack` widens the window on both sides
    to absorb the real-world variance -- an early settlement, or a bank feed
    posting a day late -- without loosening the amount tolerance, which is
    where precision would actually be lost.
    """
    target = add_banking_days(captured, cycle_days)
    earliest = target
    for _ in range(slack):
        prev = earliest - timedelta(days=1)
        while not is_banking_day(prev):
            prev -= timedelta(days=1)
        earliest = prev
    return earliest, add_banking_days(target, slack)


def date_range(start: date, end: date) -> Iterable[date]:
    """Every calendar date from `start` through `end`, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
