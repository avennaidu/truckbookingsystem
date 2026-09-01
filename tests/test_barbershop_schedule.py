"""Opening hours and slot arithmetic."""

from datetime import date

import pytest

from barbershop import schedule as sched


def test_minutes_round_trip():
    assert sched.parse_hhmm("09:00") == 540
    assert sched.hhmm(540) == "09:00"
    assert sched.friendly(540) == "9:00 AM"
    assert sched.friendly(13 * 60 + 30) == "1:30 PM"
    assert sched.friendly(12 * 60) == "12:00 PM"
    assert sched.friendly(0) == "12:00 AM"


@pytest.mark.parametrize("bad", ["9", "nine", "25:00", "09:75", ""])
def test_parse_rejects_rubbish(bad):
    with pytest.raises(ValueError):
        sched.parse_hhmm(bad)


def test_board_hours():
    # Monday closed, Tuesday to Saturday 9-7, Sunday 9-6.
    assert sched.default_hours_for(date(2026, 9, 7)) is None            # Monday
    assert sched.default_hours_for(date(2026, 9, 8)) == (540, 1140)     # Tuesday
    assert sched.default_hours_for(date(2026, 9, 12)) == (540, 1140)    # Saturday
    assert sched.default_hours_for(date(2026, 9, 13)) == (540, 1080)    # Sunday


def test_public_holiday_uses_sunday_hours_even_on_a_monday():
    monday = date(2026, 9, 7)
    assert sched.default_hours_for(monday, is_holiday=True) == (540, 1080)


def test_easter_and_south_african_holidays():
    assert sched.easter(2026) == date(2026, 4, 5)
    holidays = sched.sa_public_holidays(2026)
    assert holidays["2026-04-03"] == "Good Friday"
    assert holidays["2026-04-06"] == "Family Day"
    assert holidays["2026-12-25"] == "Christmas Day"
    # 21 March 2026 is a Saturday, so no observed Monday for it.
    assert "2026-03-23" not in holidays
    # 1 May 2033 falls on a Sunday, so the Monday is a holiday too.
    assert "2033-05-02" in sched.sa_public_holidays(2033)


def test_merge_overlapping_intervals():
    assert sched.merge([(600, 660), (630, 700), (800, 830)]) == \
        [(600, 700), (800, 830)]
    assert sched.merge([(600, 600)]) == []


def test_free_intervals_around_bookings():
    free = sched.free_intervals(540, 1140, [(600, 660), (660, 700)])
    assert free == [(540, 600), (700, 1140)]


def test_free_intervals_ignores_anything_outside_the_day():
    assert sched.free_intervals(540, 1140, [(0, 300), (1200, 1300)]) == [(540, 1140)]


def test_slots_fit_the_duration_and_land_on_the_step():
    slots = sched.slot_starts(540, 660, busy=[], duration=45, step=15)
    assert slots == [540, 555, 570, 585, 600, 615]      # last one ends at 11:00
    assert all(s + 45 <= 660 for s in slots)


def test_slots_skip_a_gap_too_short_for_the_service():
    # 09:00-10:00 free, 10:00-10:30 booked, 10:30-11:00 free: a 45 minute
    # haircut only fits in the first gap.
    slots = sched.slot_starts(540, 660, busy=[(600, 630)], duration=45, step=15)
    assert slots == [540, 555]


def test_earliest_drops_slots_that_have_gone_by():
    slots = sched.slot_starts(540, 660, busy=[], duration=30, step=15, earliest=606)
    assert slots[0] == 615


def test_a_service_needs_a_duration():
    with pytest.raises(ValueError):
        sched.slot_starts(540, 1140, [], duration=0)
