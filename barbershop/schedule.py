"""Opening hours and the slot arithmetic behind "what times are free?".

Times are minutes past midnight everywhere in here (9am is 540), because
that makes overlap checks plain integer comparisons. `hhmm()` turns them
back into something a customer reads.

The board on the wall says:

    Tuesday to Saturday   9am - 7pm
    Sunday and public holiday   9am - 6pm

which leaves Monday closed. A public holiday follows the Sunday hours even
when it lands on a Monday - Jay closes a specific day from /admin if he
would rather take it off.

Nothing in here touches the database; it takes the hours and the list of
busy intervals and does the maths, which keeps it easy to test.
"""

from datetime import date, timedelta

CLOSED = None

# Weekday (Monday is 0) -> (open, close) in minutes, or CLOSED.
DEFAULT_HOURS = {
    0: CLOSED,          # Monday
    1: (9 * 60, 19 * 60),
    2: (9 * 60, 19 * 60),
    3: (9 * 60, 19 * 60),
    4: (9 * 60, 19 * 60),
    5: (9 * 60, 19 * 60),
    6: (9 * 60, 18 * 60),   # Sunday
}
HOLIDAY_HOURS = (9 * 60, 18 * 60)


def hhmm(minutes: int) -> str:
    """540 -> '09:00'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def friendly(minutes: int) -> str:
    """540 -> '9:00 AM' - the form customers read on the booking page."""
    h, m = divmod(minutes, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def parse_hhmm(text: str) -> int:
    """'09:00' -> 540. Raises ValueError on anything else."""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"not a time of day: {text!r}")
    return hours * 60 + minutes


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1)


def sa_public_holidays(year: int) -> dict:
    """South African public holidays for `year`, keyed 'YYYY-MM-DD'.

    Where a holiday falls on a Sunday the Monday after it is a public
    holiday too, which is the rule in the Public Holidays Act.
    """
    sunday = easter(year)
    days = {
        date(year, 1, 1): "New Year's Day",
        date(year, 3, 21): "Human Rights Day",
        sunday - timedelta(days=2): "Good Friday",
        sunday + timedelta(days=1): "Family Day",
        date(year, 4, 27): "Freedom Day",
        date(year, 5, 1): "Workers' Day",
        date(year, 6, 16): "Youth Day",
        date(year, 8, 9): "National Women's Day",
        date(year, 9, 24): "Heritage Day",
        date(year, 12, 16): "Day of Reconciliation",
        date(year, 12, 25): "Christmas Day",
        date(year, 12, 26): "Day of Goodwill",
    }
    for day, name in list(days.items()):
        if day.weekday() == 6:
            days.setdefault(day + timedelta(days=1), f"{name} (observed)")
    return {d.isoformat(): name for d, name in days.items()}


def default_hours_for(day: date, is_holiday: bool = False):
    """(open, close) for a date, or None when the shop is shut."""
    if is_holiday:
        return HOLIDAY_HOURS
    return DEFAULT_HOURS[day.weekday()]


def merge(intervals):
    """Sort and merge overlapping (start, end) pairs."""
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def free_intervals(open_min: int, close_min: int, busy):
    """The gaps left in the day once `busy` is taken out."""
    free = []
    cursor = open_min
    for start, end in merge(busy):
        if end <= open_min or start >= close_min:
            continue
        start = max(start, open_min)
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, min(end, close_min))
    if cursor < close_min:
        free.append((cursor, close_min))
    return free


def slot_starts(open_min, close_min, busy, duration, step=15, earliest=None):
    """Every start time that fits a `duration` service in the free gaps.

    Starts land on `step` boundaries from the top of the hour, so the day
    reads as 9:00, 9:15, 9:30 rather than drifting by whatever the last
    appointment happened to be. `earliest` drops slots that have already
    passed (or are inside the booking lead time).
    """
    if duration <= 0:
        raise ValueError("a service needs a duration")
    starts = []
    for gap_start, gap_end in free_intervals(open_min, close_min, busy):
        first = gap_start
        if earliest is not None and first < earliest:
            first = earliest
        # round up onto the next step boundary
        if first % step:
            first += step - (first % step)
        for start in range(first, gap_end - duration + 1, step):
            if start >= gap_start:
                starts.append(start)
    return sorted(set(starts))


def overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a
