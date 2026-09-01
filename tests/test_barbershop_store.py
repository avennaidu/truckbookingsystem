"""The booking rules: one chair, real opening hours, no double bookings."""

from datetime import timedelta

import pytest

from barbershop import schedule as sched
from barbershop.store import BookingError, Store, normalise_phone


@pytest.fixture
def store(tmp_path):
    shop = Store(tmp_path / "test.db")
    yield shop
    shop.close()


def next_weekday(store, weekday, weeks_ahead=0):
    """The next date with this weekday (Monday is 0), never today."""
    day = store.today() + timedelta(days=1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return (day + timedelta(weeks=weeks_ahead)).isoformat()


def tuesday(store):
    return next_weekday(store, 1)


# ------------------------------------------------------------ the price list

def test_price_list_matches_the_board(store):
    prices = {s["id"]: s for s in store.services()}
    assert prices["haircut"]["price"] == 100
    assert prices["haircut"]["duration_min"] == 45
    assert prices["shave"]["duration_min"] == 30
    assert prices["cut-colour-set"]["duration_min"] == 90
    assert prices["package-1"]["price"] == 350
    assert prices["package-1"]["duration_min"] == 75
    assert prices["package-2"]["duration_min"] == 90


def test_prices_can_be_changed_and_stick(store):
    store.save_service("haircut", price=120, duration_min=40)
    assert store.service("haircut")["price"] == 120
    assert store.service("haircut")["duration_min"] == 40


def test_a_service_switched_off_leaves_the_customer_page(store):
    store.save_service("facial", active=0)
    assert "facial" not in {s["id"] for s in store.services()}
    assert "facial" in {s["id"] for s in store.services(active_only=False)}


def test_new_services_get_their_own_id(store):
    first = store.add_service("Beard Trim", "Cuts & Shaves", 60, 20)
    second = store.add_service("Beard Trim", "Cuts & Shaves", 60, 20)
    assert first == "beard-trim"
    assert second != first


# ----------------------------------------------------------------- the hours

def test_monday_is_closed_and_tuesday_is_not(store):
    assert store.day_info(next_weekday(store, 0))["closed"] is True
    info = store.day_info(tuesday(store))
    assert (info["open_min"], info["close_min"]) == (540, 1140)


def test_public_holidays_are_seeded_and_run_short_hours(store):
    christmas = f"{store.today().year}-12-25"
    info = store.day_info(christmas)
    assert info["holiday"] == "Christmas Day"
    if not info["closed"]:
        assert info["close_min"] == 18 * 60


def test_jay_can_close_a_single_day(store):
    day = tuesday(store)
    store.set_day(day, closed=True, note="Family day")
    assert store.day_info(day)["closed"] is True
    assert store.available(day, 45) == []
    store.clear_day(day)
    assert store.day_info(day)["closed"] is False


def test_jay_can_give_a_day_its_own_hours(store):
    day = tuesday(store)
    store.set_day(day, open_min=12 * 60, close_min=15 * 60, note="Late start")
    slots = store.available(day, 45)
    assert slots[0] == 12 * 60
    assert max(slots) + 45 <= 15 * 60


def test_closing_before_opening_is_refused(store):
    with pytest.raises(BookingError):
        store.set_day(tuesday(store), open_min=19 * 60, close_min=9 * 60)


# -------------------------------------------------------------- the bookings

def test_a_booking_takes_its_slots_off_the_board(store):
    day = tuesday(store)
    before = store.available(day, 45)
    booking = store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    after = store.available(day, 45)
    assert booking["ref"].startswith("FSJ-")
    assert booking["end_min"] == 10 * 60 + 45
    # 09:30 through 10:45 can no longer start a 45 minute haircut.
    assert set(before) - set(after) == {570, 585, 600, 615, 630}


def test_the_second_customer_cannot_take_the_same_chair(store):
    day = tuesday(store)
    store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    with pytest.raises(BookingError, match="taken"):
        store.create_booking(day, 10 * 60 + 30, "shave", "Andile", "0827654321")


def test_back_to_back_bookings_are_fine(store):
    day = tuesday(store)
    store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    second = store.create_booking(day, 10 * 60 + 45, "shave", "Andile", "0827654321")
    assert second["start_min"] == 645


def test_a_service_must_finish_before_closing(store):
    day = tuesday(store)
    # 90 minutes at 18:00 would run to 19:30, half an hour past closing.
    with pytest.raises(BookingError, match="minutes"):
        store.create_booking(day, 18 * 60, "cut-colour-set", "Zanele", "0821112222")
    late = store.create_booking(day, 17 * 60 + 30, "cut-colour-set",
                                "Zanele", "0821112222")
    assert late["end_min"] == 19 * 60


def test_the_shop_is_shut_on_mondays(store):
    with pytest.raises(BookingError, match="closed"):
        store.create_booking(next_weekday(store, 0), 10 * 60, "haircut",
                             "Sipho", "0821234567")


def test_a_name_and_number_are_needed(store):
    day = tuesday(store)
    with pytest.raises(BookingError, match="name"):
        store.create_booking(day, 10 * 60, "haircut", "", "0821234567")
    with pytest.raises(BookingError, match="number"):
        store.create_booking(day, 10 * 60, "haircut", "Sipho", "123")


def test_customers_cannot_book_beyond_the_horizon(store):
    far = (store.today() + timedelta(days=120)).isoformat()
    with pytest.raises(BookingError, match="Bookings open"):
        store.create_booking(far, 10 * 60, "haircut", "Sipho", "0821234567")


def test_yesterday_is_never_bookable(store):
    yesterday = (store.today() - timedelta(days=1)).isoformat()
    with pytest.raises(BookingError):
        store.create_booking(yesterday, 10 * 60, "haircut", "Sipho", "0821234567")


def test_jay_may_still_write_a_walk_in_outside_the_online_rules(store):
    day = tuesday(store)
    store.save_service("haircut", active=0)
    booking = store.create_booking(day, 10 * 60, "haircut", "Walk-in", "",
                                   source="walk-in", admin=True)
    assert booking["source"] == "walk-in"
    assert booking["phone"] == ""


def test_today_keeps_the_notice_period(store):
    today = store.today().isoformat()
    info = store.day_info(today)
    if info["closed"]:
        pytest.skip("the shop is closed today")
    store.set_setting("lead_time_min", 60)
    earliest = store.now_minutes() + 60
    assert store.earliest_start(today) == earliest
    assert all(s >= earliest for s in store.available(today, 30))
    # Jay writing up a walk-in is not held to the notice period.
    assert store.available(today, 30, admin=True)[0] == info["open_min"]


# ------------------------------------------------------------ blocks & moves

def test_a_blocked_hour_holds_the_chair(store):
    day = tuesday(store)
    store.add_block(day, 13 * 60, 14 * 60, "Lunch")
    assert 13 * 60 not in store.available(day, 30)
    with pytest.raises(BookingError):
        store.create_booking(day, 13 * 60 + 15, "shave", "Sipho", "0821234567")


def test_time_cannot_be_blocked_over_a_customer(store):
    day = tuesday(store)
    store.create_booking(day, 13 * 60, "haircut", "Sipho", "0821234567")
    with pytest.raises(BookingError, match="booked already"):
        store.add_block(day, 13 * 60, 14 * 60, "Lunch")


def test_freeing_a_block_puts_the_time_back(store):
    day = tuesday(store)
    block_id = store.add_block(day, 13 * 60, 14 * 60, "Lunch")
    store.remove_block(block_id)
    assert 13 * 60 in store.available(day, 30)


def test_moving_a_booking_checks_the_new_slot(store):
    day = tuesday(store)
    first = store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    store.create_booking(day, 12 * 60, "shave", "Andile", "0827654321")
    with pytest.raises(BookingError, match="chair"):
        store.move_booking(first["id"], day, 12 * 60)
    moved = store.move_booking(first["id"], day, 15 * 60)
    assert moved["start_min"] == 15 * 60
    assert 10 * 60 in store.available(day, 45)


# -------------------------------------------------------- looking things up

def test_a_customer_finds_a_booking_with_reference_and_number(store):
    day = tuesday(store)
    booking = store.create_booking(day, 10 * 60, "haircut", "Sipho", "082 123 4567")
    assert store.by_ref(booking["ref"], "0821234567")["id"] == booking["id"]
    assert store.by_ref(booking["ref"].removeprefix("FSJ-").lower(),
                        "082 123 4567") is not None
    assert store.by_ref(booking["ref"], "0829999999") is None


def test_cancelling_puts_the_slot_back(store):
    day = tuesday(store)
    booking = store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    store.cancel_by_ref(booking["ref"], "0821234567")
    assert store.booking(booking["id"])["status"] == "cancelled"
    assert 10 * 60 in store.available(day, 45)
    with pytest.raises(BookingError, match="already"):
        store.cancel_by_ref(booking["ref"], "0821234567")


def test_cancelling_needs_the_right_number(store):
    day = tuesday(store)
    booking = store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    with pytest.raises(BookingError):
        store.cancel_by_ref(booking["ref"], "0820000000")


def test_the_day_list_and_the_week_ahead(store):
    day = tuesday(store)
    store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    store.create_booking(day, 12 * 60, "shave", "Andile", "0827654321")
    assert [b["customer_name"] for b in store.bookings_for_day(day)] == \
        ["Sipho", "Andile"]
    assert len(store.upcoming(days=30)) == 2


def test_takings_count_only_finished_appointments(store):
    day = tuesday(store)
    first = store.create_booking(day, 10 * 60, "haircut", "Sipho", "0821234567")
    store.create_booking(day, 12 * 60, "shave", "Andile", "0827654321")
    assert store.takings(day, day)["rand"] == 0
    store.set_status(first["id"], "completed")
    takings = store.takings(day, day)
    assert takings["rand"] == 100 and takings["jobs"] == 1
    assert takings["services"][0]["service_name"] == "Haircut"


def test_phone_numbers_are_compared_without_the_spaces():
    assert normalise_phone("082 123 4567") == "0821234567"
    assert normalise_phone("+27 82 123 4567") == "+27821234567"
    assert normalise_phone(None) == ""


def test_the_booking_row_is_ready_to_show(store):
    booking = store.create_booking(tuesday(store), 10 * 60, "haircut",
                                   "Sipho", "0821234567")
    assert booking["time"] == "10:00 AM"
    assert booking["end_time"] == sched.friendly(10 * 60 + 45)
    assert booking["status"] == "booked"
    assert booking["price"] == 100
