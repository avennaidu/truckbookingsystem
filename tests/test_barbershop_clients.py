"""Client accounts and standing appointments."""

from datetime import date, timedelta

import pytest

from barbershop.store import MAX_REPEATS, BookingError, Store, check_pin, hash_pin


@pytest.fixture
def store(tmp_path):
    shop = Store(tmp_path / "clients.db")
    yield shop
    shop.close()


@pytest.fixture
def sipho(store):
    return store.register("Sipho", "082 123 4567", "1234")


def tuesday(store, weeks=0):
    day = store.today() + timedelta(days=1)
    while day.weekday() != 1:
        day += timedelta(days=1)
    return (day + timedelta(weeks=weeks)).isoformat()


# ------------------------------------------------------------------ accounts

def test_a_pin_is_hashed_not_kept(store, sipho):
    row = store.db.execute("SELECT pin_hash FROM customers WHERE id = ?",
                           (sipho["id"],)).fetchone()["pin_hash"]
    assert "1234" not in row
    assert check_pin("1234", row) and not check_pin("1235", row)


def test_two_hashes_of_one_pin_differ(store):
    assert hash_pin("1234") != hash_pin("1234")


def test_the_number_is_the_account_name(store, sipho):
    assert sipho["phone"] == "0821234567"
    assert store.customer_by_phone("082 123 4567")["id"] == sipho["id"]
    with pytest.raises(BookingError, match="already has an account"):
        store.register("Someone Else", "0821234567", "9999")


def test_signing_in_takes_the_number_however_it_is_typed(store, sipho):
    assert store.sign_in("0821234567", "1234")["id"] == sipho["id"]
    assert store.sign_in("082 123 4567", "1234")["id"] == sipho["id"]
    assert store.sign_in("0821234567", "9999") is None
    assert store.sign_in("0820000000", "1234") is None


@pytest.mark.parametrize("bad", ["123", "123456789", "abcd", "", "12 34"])
def test_a_pin_has_to_be_four_to_eight_numbers(store, bad):
    with pytest.raises(BookingError, match="PIN"):
        store.register("Sipho", "0829990000", bad)


def test_an_account_needs_a_name_and_a_real_number(store):
    with pytest.raises(BookingError, match="name"):
        store.register("", "0821234567", "1234")
    with pytest.raises(BookingError, match="number"):
        store.register("Sipho", "123", "1234")


def test_a_session_signs_someone_in_and_out(store, sipho):
    token = store.start_session(sipho["id"])
    assert store.session_customer(token)["id"] == sipho["id"]
    store.end_session(token)
    assert store.session_customer(token) is None
    assert store.session_customer("nonsense") is None
    assert store.session_customer(None) is None


def test_an_expired_session_stops_working(store, sipho):
    token = store.start_session(sipho["id"])
    store.db.execute("UPDATE client_sessions SET expires_at = ? WHERE token = ?",
                     ("2020-01-01T00:00:00", token))
    store.db.commit()
    assert store.session_customer(token) is None


def test_a_new_pin_signs_the_other_phones_out(store, sipho):
    phone_one = store.start_session(sipho["id"])
    phone_two = store.start_session(sipho["id"])
    store.update_customer(sipho["id"], pin="4821")
    store.end_all_sessions(sipho["id"])
    assert store.session_customer(phone_one) is None
    assert store.session_customer(phone_two) is None
    assert store.sign_in("0821234567", "4821")["id"] == sipho["id"]


def test_jay_sees_who_has_registered(store, sipho):
    store.register("Andile", "0827654321", "1234")
    booking = store.create_booking(tuesday(store), 10 * 60, "haircut",
                                   sipho["name"], sipho["phone"],
                                   customer_id=sipho["id"])
    store.set_status(booking["id"], "completed")
    listed = {c["name"]: c for c in store.customers()}
    assert set(listed) == {"Sipho", "Andile"}
    assert listed["Sipho"]["visits"] == 1
    assert [c["name"] for c in store.customers("andile")] == ["Andile"]
    assert [c["name"] for c in store.customers("0821234567")] == ["Sipho"]


# -------------------------------------------------------- a customer's diary

def test_a_customer_sees_only_their_own_appointments(store, sipho):
    andile = store.register("Andile", "0827654321", "1234")
    day = tuesday(store)
    store.create_booking(day, 10 * 60, "haircut", sipho["name"], sipho["phone"],
                         customer_id=sipho["id"])
    store.create_booking(day, 12 * 60, "shave", andile["name"], andile["phone"],
                         customer_id=andile["id"])
    assert [b["service_name"] for b in store.customer_bookings(sipho["id"])] \
        == ["Haircut"]
    assert [b["service_name"] for b in store.customer_bookings(andile["id"])] \
        == ["Shave"]


def test_a_customer_cancels_their_own_and_nobody_else_s(store, sipho):
    andile = store.register("Andile", "0827654321", "1234")
    day = tuesday(store)
    mine = store.create_booking(day, 10 * 60, "haircut", sipho["name"],
                                sipho["phone"], customer_id=sipho["id"])
    with pytest.raises(BookingError, match="not on your account"):
        store.cancel_own(andile["id"], mine["id"])
    store.cancel_own(sipho["id"], mine["id"])
    assert store.booking(mine["id"])["status"] == "cancelled"
    assert 10 * 60 in store.available(day, 45)


def test_past_visits_are_kept_apart_from_what_is_coming(store, sipho):
    day = tuesday(store)
    booking = store.create_booking(day, 10 * 60, "haircut", sipho["name"],
                                   sipho["phone"], customer_id=sipho["id"])
    store.set_status(booking["id"], "completed")
    assert store.customer_bookings(sipho["id"]) == []
    assert len(store.customer_bookings(sipho["id"], upcoming=False)) == 1


# ------------------------------------------------------------------- repeats

def test_the_dates_of_a_repeat_are_the_same_weekday(store):
    days = store.repeat_dates("2026-09-08", every_weeks=2, times=4)
    assert days == ["2026-09-08", "2026-09-22", "2026-10-06", "2026-10-20"]
    assert all(date.fromisoformat(d).weekday() == 1 for d in days)


@pytest.mark.parametrize("every,times", [(5, 4), (0, 4), (2, 1), (2, 20)])
def test_a_repeat_stays_inside_sensible_limits(store, every, times):
    with pytest.raises(BookingError):
        store.repeat_dates(tuesday(store), every, times)


def test_a_repeat_books_every_date_when_the_chair_is_free(store, sipho):
    first = tuesday(store)
    result = store.create_repeat(sipho["id"], "haircut", first, 10 * 60,
                                 every_weeks=2, times=4)
    assert len(result["booked"]) == 4 and result["missed"] == []
    assert [b["day"] for b in result["booked"]] == \
        store.repeat_dates(first, 2, 4)
    assert all(b["source"] == "repeat" and b["repeat"] for b in result["booked"])
    assert all(b["customer_id"] == sipho["id"] for b in result["booked"])


def test_a_repeat_reaches_past_the_normal_booking_window(store, sipho):
    # Twelve fortnightly cuts run most of a year - further than the 60 days a
    # single booking may be made, which is the point of a standing slot.
    first = tuesday(store)
    result = store.create_repeat(sipho["id"], "haircut", first, 10 * 60,
                                 every_weeks=2, times=MAX_REPEATS)
    assert len(result["booked"]) == MAX_REPEATS
    _, horizon = store.horizon()
    assert date.fromisoformat(result["booked"][-1]["day"]) > horizon


def test_a_repeat_skips_a_date_that_is_taken_and_says_so(store, sipho):
    first = tuesday(store)
    clash = store.repeat_dates(first, 2, 4)[2]
    store.create_booking(clash, 10 * 60, "shave", "Andile", "0827654321")
    result = store.create_repeat(sipho["id"], "haircut", first, 10 * 60,
                                 every_weeks=2, times=4)
    assert len(result["booked"]) == 3
    assert [m["day"] for m in result["missed"]] == [clash]
    assert "taken" in result["missed"][0]["reason"]


def test_a_repeat_skips_a_day_the_shop_is_closed(store, sipho):
    first = tuesday(store)
    shut = store.repeat_dates(first, 1, 3)[1]
    store.set_day(shut, closed=True, note="Family day")
    result = store.create_repeat(sipho["id"], "haircut", first, 10 * 60,
                                 every_weeks=1, times=3)
    assert [m["day"] for m in result["missed"]] == [shut]
    assert "closed" in result["missed"][0]["reason"]


def test_a_repeat_that_fits_nowhere_is_refused(store, sipho):
    first = tuesday(store)
    for day in store.repeat_dates(first, 2, 3):
        store.set_day(day, closed=True)
    with pytest.raises(BookingError, match="None of those dates"):
        store.create_repeat(sipho["id"], "haircut", first, 10 * 60, 2, 3)
    assert store.series_for_customer(sipho["id"]) == []


def test_a_repeat_will_not_start_past_closing_time(store, sipho):
    with pytest.raises(BookingError, match="None of those dates"):
        store.create_repeat(sipho["id"], "cut-colour-set", tuesday(store),
                            18 * 60 + 30, 2, 3)


def test_the_plan_shows_what_would_happen_before_anything_is_written(store, sipho):
    first = tuesday(store)
    taken = store.repeat_dates(first, 1, 3)[1]
    store.create_booking(taken, 10 * 60, "shave", "Andile", "0827654321")
    plan = store.plan_repeat("haircut", first, 10 * 60, 1, 3)
    assert plan["free"] == 2
    assert [entry["ok"] for entry in plan["plan"]] == [True, False, True]
    assert store.customer_bookings(sipho["id"]) == []      # nothing was booked


def test_a_repeat_shows_up_on_the_customer_account(store, sipho):
    first = tuesday(store)
    store.create_repeat(sipho["id"], "haircut", first, 10 * 60, 2, 4)
    repeats = store.series_for_customer(sipho["id"])
    assert len(repeats) == 1
    series = repeats[0]
    assert series["service_name"] == "Haircut"
    assert series["every"] == "every 2 weeks"
    assert series["weekday"] == "Tuesday"
    assert series["time"] == "10:00 AM"
    assert len(series["remaining"]) == 4
    assert len(store.customer_bookings(sipho["id"])) == 4


def test_stopping_a_repeat_cancels_what_is_left(store, sipho):
    first = tuesday(store)
    result = store.create_repeat(sipho["id"], "haircut", first, 10 * 60, 2, 4)
    store.set_status(result["booked"][0]["id"], "completed")
    stopped = store.cancel_series(result["series"]["id"], sipho["id"])
    assert stopped["cancelled"] == 3
    assert stopped["series"]["status"] == "cancelled"
    assert store.booking(result["booked"][0]["id"])["status"] == "completed"
    assert store.customer_bookings(sipho["id"]) == []
    assert 10 * 60 in store.available(result["booked"][1]["day"], 45)


def test_one_customer_cannot_stop_another_customer_repeat(store, sipho):
    andile = store.register("Andile", "0827654321", "1234")
    result = store.create_repeat(sipho["id"], "haircut", tuesday(store),
                                 10 * 60, 2, 3)
    with pytest.raises(BookingError, match="not on your account"):
        store.cancel_series(result["series"]["id"], andile["id"])
    assert len(store.customer_bookings(sipho["id"])) == 3
