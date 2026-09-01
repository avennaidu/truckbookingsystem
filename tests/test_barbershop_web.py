"""The web layer, driven the way a phone drives it."""

import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from barbershop.store import Store
from barbershop.webapp import Server


@pytest.fixture
def shop(tmp_path):
    store = Store(tmp_path / "web.db")
    server = Server(("127.0.0.1", 0), store, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield Client(base, store)
    server.shutdown()
    server.server_close()
    store.close()


class Client:
    """Just enough of a browser: JSON in, JSON out, and it keeps the cookie."""

    def __init__(self, base, store):
        self.base = base
        self.store = store
        self.cookies = {}          # a browser holds both cookies at once

    def request(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method or ("POST" if data else "GET"),
            headers={"Content-Type": "application/json"})
        if self.cookies:
            request.add_header("Cookie", "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()))
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        raw = response.read()
        for set_cookie in response.headers.get_all("Set-Cookie") or []:
            name, _, value = set_cookie.split(";")[0].partition("=")
            if value:
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        if not response.headers.get("Content-Type", "").startswith("application/json"):
            return response.status, raw.decode()
        return response.status, json.loads(raw)

    def ok(self, path, body=None, method=None):
        status, data = self.request(path, body, method)
        assert status == 200 and data.get("ok"), data
        return data

    def another(self):
        """A second browser - its own cookies, the same server."""
        return Client(self.base, self.store)

    def register(self, name="Sipho", phone="0821234567", pin="1234"):
        return self.ok("/api/register",
                       {"name": name, "phone": phone, "pin": pin})["customer"]

    def tuesday(self):
        day = self.store.today() + timedelta(days=1)
        while day.weekday() != 1:
            day += timedelta(days=1)
        return day.isoformat()


# ---------------------------------------------------------------- customers

def test_the_pages_are_served(shop):
    for path in ("/", "/admin"):
        status, html = shop.request(path)
        assert status == 200 and "Faded Studio" in html
    status, css = shop.request("/static/brand.css")
    assert status == 200


def test_static_files_cannot_escape_the_folder(shop):
    status, _ = shop.request("/static/../store.py")
    assert status == 404


def test_the_price_list_is_public(shop):
    services = shop.ok("/api/services")["services"]
    haircut = next(s for s in services if s["id"] == "haircut")
    assert (haircut["price"], haircut["duration_min"]) == (100, 45)


def test_the_calendar_marks_mondays_closed(shop):
    data = shop.ok(f"/api/days?service=haircut&from={shop.store.today()}&days=14")
    mondays = [d for d in data["days"] if d["weekday"] == 0 and not d["holiday"]]
    assert mondays and all(d["closed"] and d["free"] == 0 for d in mondays)


def test_slots_then_a_booking_then_the_slot_is_gone(shop):
    day = shop.tuesday()
    shop.register()
    slots = shop.ok(f"/api/slots?service=haircut&day={day}")["slots"]
    assert slots[0]["time"] == "9:00 AM"
    booking = shop.ok("/api/book", {"day": day, "start": slots[0]["start"],
                                    "service": "haircut"})["booking"]
    assert booking["ref"].startswith("FSJ-")
    left = shop.ok(f"/api/slots?service=haircut&day={day}")["slots"]
    assert slots[0]["start"] not in [s["start"] for s in left]


def test_the_same_slot_twice_is_refused_politely(shop):
    day = shop.tuesday()
    body = {"day": day, "start": 10 * 60, "service": "haircut"}
    shop.register()
    shop.ok("/api/book", body)
    other = shop.another()
    other.register(name="Andile", phone="0827654321")
    status, data = other.request("/api/book", body)
    assert status == 400 and "taken" in data["error"]


def test_a_customer_sees_and_cancels_their_own_booking(shop):
    day = shop.tuesday()
    shop.register()
    booking = shop.ok("/api/book", {"day": day, "start": 10 * 60,
                                    "service": "shave"})["booking"]
    me = shop.ok("/api/me")
    assert me["customer"]["name"] == "Sipho"
    assert [b["ref"] for b in me["upcoming"]] == [booking["ref"]]
    assert shop.ok("/api/cancel", {"id": booking["id"]})["booking"]["status"] \
        == "cancelled"
    assert shop.ok("/api/me")["upcoming"] == []


def test_one_customer_cannot_cancel_another_customer_booking(shop):
    day = shop.tuesday()
    shop.register()
    booking = shop.ok("/api/book", {"day": day, "start": 10 * 60,
                                    "service": "shave"})["booking"]
    other = shop.another()
    other.register(name="Andile", phone="0827654321")
    status, data = other.request("/api/cancel", {"id": booking["id"]})
    assert status == 400 and "not on your account" in data["error"]


def test_rubbish_input_gets_an_error_not_a_stack_trace(shop):
    shop.register()
    status, data = shop.request("/api/book", {"day": "not-a-date", "start": 600,
                                              "service": "haircut"})
    assert status == 400 and "error" in data
    status, data = shop.request("/api/slots?service=nonsense")
    assert status == 400


# -------------------------------------------------------------------- admin

def test_the_diary_is_behind_the_pin(shop):
    status, _ = shop.request(f"/api/admin/day?day={shop.tuesday()}")
    assert status == 401
    status, _ = shop.request("/api/admin/login", {"pin": "0000"})
    assert status == 401
    shop.ok("/api/admin/login", {"pin": "1234"})
    assert shop.ok("/api/admin/session")["signed_in"] is True
    shop.ok(f"/api/admin/day?day={shop.tuesday()}")


def test_jay_works_a_day_from_the_diary(shop):
    day = shop.tuesday()
    shop.ok("/api/admin/login", {"pin": "1234"})
    booking = shop.ok("/api/admin/booking", {
        "day": day, "start": 9 * 60 + 5, "service": "haircut",
        "name": "Walk-in", "phone": "", "source": "walk-in"})["booking"]
    shop.ok("/api/admin/block", {"day": day, "start": "13:00", "end": "13:30",
                                 "reason": "Lunch"})
    view = shop.ok(f"/api/admin/day?day={day}")
    assert view["expected"] == 100
    assert view["blocks"][0]["start"] == "1:00 PM"
    shop.ok("/api/admin/status", {"id": booking["id"], "status": "completed"})
    takings = shop.ok(f"/api/admin/takings?first={day}&last={day}")["takings"]
    assert takings["rand"] == 100


def test_jay_moves_a_booking(shop):
    day = shop.tuesday()
    shop.register()
    shop.ok("/api/admin/login", {"pin": "1234"})
    booking = shop.ok("/api/book", {"day": day, "start": 10 * 60,
                                    "service": "haircut"})["booking"]
    moved = shop.ok("/api/admin/move", {"id": booking["id"], "day": day,
                                        "start": 15 * 60})["booking"]
    assert moved["time"] == "3:00 PM"


def test_jay_closes_a_day_and_customers_see_it(shop):
    day = shop.tuesday()
    shop.ok("/api/admin/login", {"pin": "1234"})
    shop.ok("/api/admin/hours", {"day": day, "closed": True, "note": "Family day"})
    slots = shop.ok(f"/api/slots?service=haircut&day={day}")
    assert slots["closed"] is True and slots["slots"] == []
    shop.ok("/api/admin/hours", {"day": day, "clear": True})
    assert shop.ok(f"/api/slots?service=haircut&day={day}")["slots"]


def test_jay_changes_a_price_and_the_customer_page_follows(shop):
    shop.ok("/api/admin/login", {"pin": "1234"})
    shop.ok("/api/admin/service", {"id": "haircut", "price": 120})
    haircut = next(s for s in shop.ok("/api/services")["services"]
                   if s["id"] == "haircut")
    assert haircut["price"] == 120
    shop.ok("/api/admin/service", {"id": "shave", "active": 0})
    assert "shave" not in {s["id"] for s in shop.ok("/api/services")["services"]}


def test_changing_the_pin_signs_the_old_one_out(shop):
    shop.ok("/api/admin/login", {"pin": "1234"})
    shop.ok("/api/admin/pin", {"pin": "4821"})
    shop.ok("/api/admin/logout", method="POST")
    status, _ = shop.request("/api/admin/login", {"pin": "1234"})
    assert status == 401
    shop.ok("/api/admin/login", {"pin": "4821"})


def test_settings_are_checked(shop):
    shop.ok("/api/admin/login", {"pin": "1234"})
    status, data = shop.request("/api/admin/settings", {"slot_step_min": 7})
    assert status == 400 and "5, 10, 15" in data["error"]
    settings = shop.ok("/api/admin/settings", {"lead_time_min": 45})["settings"]
    assert settings["lead_time_min"] == 45


# ------------------------------------------------------------- the portal

def test_nobody_books_without_an_account(shop):
    day = shop.tuesday()
    status, data = shop.request("/api/book", {"day": day, "start": 10 * 60,
                                              "service": "haircut"})
    assert status == 401 and "sign in" in data["error"]
    assert shop.ok("/api/me")["signed_in"] is False


def test_registering_signs_you_straight_in(shop):
    customer = shop.register(name="Sipho", phone="082 123 4567", pin="1234")
    assert customer["phone"] == "0821234567"
    assert shop.ok("/api/me")["customer"]["name"] == "Sipho"
    status, data = shop.request("/api/register",
                                {"name": "Someone", "phone": "0821234567",
                                 "pin": "9999"})
    assert status == 400 and "already has an account" in data["error"]


def test_signing_in_from_another_phone(shop):
    shop.register()
    other = shop.another()
    status, _ = other.request("/api/login", {"phone": "0821234567", "pin": "9999"})
    assert status == 401
    other.ok("/api/login", {"phone": "082 123 4567", "pin": "1234"})
    assert other.ok("/api/me")["customer"]["name"] == "Sipho"


def test_guessing_at_a_pin_gets_shut_out(shop):
    shop.register()
    guesser = shop.another()
    codes = [guesser.request("/api/login",
                             {"phone": "0821234567", "pin": f"{n:04d}"})[0]
             for n in range(8)]
    assert codes[0] == 401 and codes[-1] == 429
    # The real PIN is refused too while the cool-off is running.
    assert guesser.request("/api/login",
                           {"phone": "0821234567", "pin": "1234"})[0] == 429


def test_signing_out_ends_the_session(shop):
    shop.register()
    shop.ok("/api/logout", method="POST")
    assert shop.ok("/api/me")["signed_in"] is False


def test_a_customer_changes_their_name_and_pin(shop):
    shop.register()
    shop.ok("/api/account", {"name": "Sipho M"})
    assert shop.ok("/api/me")["customer"]["name"] == "Sipho M"
    status, _ = shop.request("/api/account", {"old_pin": "0000", "pin": "4821"})
    assert status == 401
    shop.ok("/api/account", {"old_pin": "1234", "pin": "4821"})
    assert shop.ok("/api/me")["signed_in"] is True      # this phone stays in
    other = shop.another()
    other.ok("/api/login", {"phone": "0821234567", "pin": "4821"})


def test_the_booking_carries_the_account_name_and_number(shop):
    shop.register(name="Sipho", phone="0821234567")
    booking = shop.ok("/api/book", {"day": shop.tuesday(), "start": 10 * 60,
                                    "service": "haircut"})["booking"]
    assert booking["customer_name"] == "Sipho"
    assert booking["phone"] == "0821234567"


# ------------------------------------------------------- standing bookings

def test_a_repeat_is_previewed_before_it_is_booked(shop):
    day = shop.tuesday()
    shop.register()
    plan = shop.ok("/api/repeat/preview", {"service": "haircut", "day": day,
                                           "start": 10 * 60, "every_weeks": 2,
                                           "times": 4})
    assert plan["free"] == 4 and len(plan["plan"]) == 4
    assert shop.ok("/api/me")["upcoming"] == []          # nothing booked yet


def test_a_repeat_books_the_run_and_shows_on_the_account(shop):
    day = shop.tuesday()
    shop.register()
    result = shop.ok("/api/repeat", {"service": "haircut", "day": day,
                                     "start": 10 * 60, "every_weeks": 2,
                                     "times": 4})
    assert len(result["booked"]) == 4 and result["missed"] == []
    me = shop.ok("/api/me")
    assert len(me["upcoming"]) == 4
    assert me["repeats"][0]["every"] == "every 2 weeks"
    assert all(b["repeat"] for b in me["upcoming"])


def test_stopping_a_repeat_from_the_portal(shop):
    day = shop.tuesday()
    shop.register()
    result = shop.ok("/api/repeat", {"service": "haircut", "day": day,
                                     "start": 10 * 60, "every_weeks": 2,
                                     "times": 3})
    stopped = shop.ok("/api/repeat/cancel", {"id": result["series"]["id"]})
    assert stopped["cancelled"] == 3
    assert shop.ok("/api/me")["upcoming"] == []


def test_a_repeat_needs_an_account_too(shop):
    status, _ = shop.request("/api/repeat", {"service": "haircut",
                                             "day": shop.tuesday(),
                                             "start": 600, "every_weeks": 2,
                                             "times": 3})
    assert status == 401


def test_jay_sees_the_client_list_and_resets_a_pin(shop):
    shop.register(name="Sipho", phone="0821234567", pin="1234")
    shop.ok("/api/admin/login", {"pin": "1234"})
    clients = shop.ok("/api/admin/clients")["clients"]
    assert [c["phone"] for c in clients] == ["0821234567"]
    shop.ok("/api/admin/client-pin", {"id": clients[0]["id"], "pin": "5555"})
    phone = shop.another()
    assert phone.request("/api/login", {"phone": "0821234567", "pin": "1234"})[0] \
        == 401
    phone.ok("/api/login", {"phone": "0821234567", "pin": "5555"})


def test_the_client_list_is_behind_the_shop_pin(shop):
    status, _ = shop.request("/api/admin/clients")
    assert status == 401
