"""The web server: a booking page for customers, a diary for Jay.

    python -m barbershop serve --port 8080

Everything is JSON over a handful of routes; the two pages in static/ do
the rest in the browser. Admin routes sit behind a PIN - Jay logs in once
on his phone and the session cookie keeps him there.
"""

import json
import mimetypes
import secrets
import threading
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import SHOP, __version__
from . import schedule as sched
from .store import BookingError, Store

STATIC = Path(__file__).parent / "static"
COOKIE = "faded_admin"
MAX_BODY = 64 * 1024


class Sessions:
    """Admin logins, in memory - restarting the server signs Jay out."""

    def __init__(self, hours=12):
        self._tokens = {}
        self._lock = threading.Lock()
        self.ttl = hours * 3600

    def new(self, now):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = now + self.ttl
        return token

    def valid(self, token, now):
        if not token:
            return False
        with self._lock:
            expires = self._tokens.get(token)
            if expires is None:
                return False
            if expires < now:
                del self._tokens[token]
                return False
        return True

    def drop(self, token):
        with self._lock:
            self._tokens.pop(token, None)


class Handler(BaseHTTPRequestHandler):
    server_version = f"FadedStudio/{__version__}"
    store: Store
    sessions: Sessions

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        self.server.log(f"{self.address_string()} {fmt % args}")

    def _send(self, code, body, content_type="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, payload, code=200, headers=None):
        self._send(code, json.dumps(payload, default=str), headers=headers)

    def fail(self, message, code=400):
        self.json({"ok": False, "error": message}, code)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise BookingError("That request is too big.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BookingError("Could not read that request.")
        if not isinstance(data, dict):
            raise BookingError("Could not read that request.")
        return data

    def token(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get(COOKIE)
        return morsel.value if morsel else None

    def is_admin(self):
        import time
        return self.sessions.valid(self.token(), time.time())

    def require_admin(self):
        if self.is_admin():
            return True
        self.fail("Please sign in again.", 401)
        return False

    # --------------------------------------------------------------- routes

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        try:
            if path == "/":
                return self.page("book.html")
            if path == "/admin":
                return self.page("admin.html")
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            if path == "/api/shop":
                return self.api_shop()
            if path == "/api/services":
                return self.json({"ok": True, "services": self.store.services()})
            if path == "/api/days":
                return self.api_days(query)
            if path == "/api/slots":
                return self.api_slots(query)
            if path == "/api/admin/session":
                return self.json({"ok": True, "signed_in": self.is_admin()})
            if path == "/api/admin/day":
                return self.api_admin_day(query)
            if path == "/api/admin/upcoming":
                return self.api_admin_upcoming(query)
            if path == "/api/admin/takings":
                return self.api_admin_takings(query)
            if path == "/api/admin/services":
                if not self.require_admin():
                    return
                return self.json({"ok": True,
                                  "services": self.store.services(active_only=False)})
            if path == "/api/admin/calendar":
                return self.api_admin_calendar(query)
            return self.fail("No such page.", 404)
        except BookingError as exc:
            return self.fail(str(exc))
        except Exception as exc:                       # noqa: BLE001
            self.server.log(f"error on GET {self.path}: {exc!r}")
            return self.fail("Something went wrong at the shop's end.", 500)

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            data = self.body()
            if path == "/api/book":
                return self.api_book(data)
            if path == "/api/lookup":
                return self.api_lookup(data)
            if path == "/api/cancel":
                return self.api_cancel(data)
            if path == "/api/admin/login":
                return self.api_login(data)
            if path == "/api/admin/logout":
                self.sessions.drop(self.token())
                return self.json({"ok": True}, headers={
                    "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            if not path.startswith("/api/admin/"):
                return self.fail("No such page.", 404)
            if not self.require_admin():
                return
            action = path[len("/api/admin/"):]
            handlers = {
                "booking": self.api_admin_booking,
                "status": self.api_admin_status,
                "move": self.api_admin_move,
                "block": self.api_admin_block,
                "unblock": self.api_admin_unblock,
                "hours": self.api_admin_hours,
                "holiday": self.api_admin_holiday,
                "service": self.api_admin_service,
                "new-service": self.api_admin_new_service,
                "pin": self.api_admin_pin,
                "settings": self.api_admin_settings,
            }
            if action in handlers:
                return handlers[action](data)
            return self.fail("No such page.", 404)
        except BookingError as exc:
            return self.fail(str(exc))
        except Exception as exc:                       # noqa: BLE001
            self.server.log(f"error on POST {self.path}: {exc!r}")
            return self.fail("Something went wrong at the shop's end.", 500)

    # ---------------------------------------------------------------- pages

    def page(self, name):
        try:
            html = (STATIC / name).read_bytes()
        except OSError:
            return self.fail("Page missing from the install.", 500)
        self._send(200, html, "text/html; charset=utf-8")

    def static(self, name):
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self.fail("Not found.", 404)
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)

    # ------------------------------------------------------------ public API

    def api_shop(self):
        store = self.store
        first, last = store.horizon()
        self.json({"ok": True, "shop": SHOP, "version": __version__,
                   "today": store.today().isoformat(),
                   "now_min": store.now_minutes(),
                   "first_day": first.isoformat(), "last_day": last.isoformat()})

    def _day_arg(self, query, key="day", default=None):
        value = (query.get(key) or [default or self.store.today().isoformat()])[0]
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise BookingError("That is not a valid date.")

    def _service_arg(self, query):
        service_id = (query.get("service") or [""])[0]
        service = self.store.service(service_id)
        if not service:
            raise BookingError("Please choose a service first.")
        return service

    def api_days(self, query):
        """A month of days with a yes/no on whether anything is free."""
        service = self._service_arg(query)
        start = self._day_arg(query, "from")
        count = min(int((query.get("days") or [42])[0]), 120)
        first, last = self.store.horizon()
        days = []
        for offset in range(count):
            day = start + timedelta(days=offset)
            info = self.store.day_info(day.isoformat())
            bookable = first <= day <= last
            slots = ([] if info["closed"] or not bookable
                     else self.store.available(day.isoformat(),
                                               service["duration_min"]))
            days.append({
                "day": day.isoformat(),
                "dom": day.day,
                "weekday": day.weekday(),
                "closed": info["closed"],
                "holiday": info["holiday"],
                "reason": info["reason"],
                "in_range": bookable,
                "free": len(slots),
            })
        self.json({"ok": True, "service": service, "days": days})

    def api_slots(self, query):
        service = self._service_arg(query)
        day = self._day_arg(query)
        info = self.store.day_info(day.isoformat())
        starts = self.store.available(day.isoformat(), service["duration_min"])
        self.json({"ok": True, "service": service, "day": day.isoformat(),
                   "label": info["label"], "closed": info["closed"],
                   "reason": info["reason"], "holiday": info["holiday"],
                   "hours": (None if info["closed"] else
                             f"{sched.friendly(info['open_min'])} - "
                             f"{sched.friendly(info['close_min'])}"),
                   "slots": [{"start": s, "time": sched.friendly(s),
                              "end": sched.friendly(s + service["duration_min"])}
                             for s in starts]})

    def api_book(self, data):
        booking = self.store.create_booking(
            day=data.get("day"), start_min=int(data.get("start", -1)),
            service_id=data.get("service"), customer_name=data.get("name"),
            phone=data.get("phone"), notes=data.get("notes", ""),
            source="online")
        self.server.log(f"booked {booking['ref']} {booking['day']} "
                        f"{booking['time']} {booking['service_name']}")
        self.json({"ok": True, "booking": booking, "shop": SHOP})

    def api_lookup(self, data):
        booking = self.store.by_ref(data.get("ref"), data.get("phone"))
        if not booking:
            return self.fail("No booking matches that reference and number.", 404)
        self.json({"ok": True, "booking": booking})

    def api_cancel(self, data):
        booking = self.store.cancel_by_ref(data.get("ref"), data.get("phone"))
        self.server.log(f"cancelled {booking['ref']}")
        self.json({"ok": True, "booking": booking})

    # ------------------------------------------------------------- admin API

    def api_login(self, data):
        import time
        pin = str(data.get("pin", "")).strip()
        if not secrets.compare_digest(pin, self.store.setting("admin_pin", "")):
            self.server.log("failed admin sign-in")
            return self.fail("That PIN is not right.", 401)
        token = self.sessions.new(time.time())
        self.json({"ok": True}, headers={
            "Set-Cookie": f"{COOKIE}={token}; Path=/; Max-Age={self.sessions.ttl};"
                          " HttpOnly; SameSite=Lax"})

    def api_admin_day(self, query):
        if not self.require_admin():
            return
        day = self._day_arg(query)
        info = self.store.day_info(day.isoformat())
        bookings = self.store.bookings_for_day(day.isoformat())
        live = [b for b in bookings if b["status"] in ("booked", "confirmed")]
        self.json({
            "ok": True, "day": day.isoformat(), "info": info,
            "hours": (None if info["closed"] else
                      f"{sched.friendly(info['open_min'])} - "
                      f"{sched.friendly(info['close_min'])}"),
            "bookings": bookings,
            "blocks": [dict(b, start=sched.friendly(b["start_min"]),
                            end=sched.friendly(b["end_min"]))
                       for b in self.store.blocks(day.isoformat())],
            "expected": sum(b["price"] for b in live),
            "booked_minutes": sum(b["duration_min"] for b in live),
            "prev": (day - timedelta(days=1)).isoformat(),
            "next": (day + timedelta(days=1)).isoformat(),
            "is_today": day == self.store.today(),
            "now_min": self.store.now_minutes(),
        })

    def api_admin_upcoming(self, query):
        if not self.require_admin():
            return
        days = min(int((query.get("days") or [14])[0]), 120)
        self.json({"ok": True, "bookings": self.store.upcoming(days=days)})

    def api_admin_takings(self, query):
        if not self.require_admin():
            return
        last = self._day_arg(query, "last")
        first = self._day_arg(query, "first",
                              (last - timedelta(days=29)).isoformat())
        self.json({"ok": True, "takings": self.store.takings(first, last)})

    def api_admin_calendar(self, query):
        """Booking counts per day, to shade the diary's month view."""
        if not self.require_admin():
            return
        start = self._day_arg(query, "from")
        count = min(int((query.get("days") or [42])[0]), 120)
        days = []
        for offset in range(count):
            day = (start + timedelta(days=offset)).isoformat()
            info = self.store.day_info(day)
            live = self.store.bookings_for_day(day, live_only=True)
            days.append({"day": day, "dom": int(day[-2:]),
                         "closed": info["closed"], "holiday": info["holiday"],
                         "bookings": len(live),
                         "rand": sum(b["price"] for b in live)})
        self.json({"ok": True, "days": days})

    def api_admin_booking(self, data):
        booking = self.store.create_booking(
            day=data.get("day"), start_min=int(data.get("start", -1)),
            service_id=data.get("service"), customer_name=data.get("name"),
            phone=data.get("phone", ""), notes=data.get("notes", ""),
            source=data.get("source", "walk-in"), admin=True)
        self.json({"ok": True, "booking": booking})

    def api_admin_status(self, data):
        booking = self.store.set_status(data.get("id"), data.get("status", ""))
        self.json({"ok": True, "booking": booking})

    def api_admin_move(self, data):
        booking = self.store.move_booking(
            data.get("id"), data.get("day"), int(data.get("start", -1)))
        self.json({"ok": True, "booking": booking})

    def api_admin_block(self, data):
        block_id = self.store.add_block(
            data.get("day"), sched.parse_hhmm(data.get("start")),
            sched.parse_hhmm(data.get("end")), str(data.get("reason", "")).strip())
        self.json({"ok": True, "id": block_id})

    def api_admin_unblock(self, data):
        self.store.remove_block(data.get("id"))
        self.json({"ok": True})

    def api_admin_hours(self, data):
        day = str(data.get("day", ""))
        if data.get("clear"):
            self.store.clear_day(day)
        elif data.get("closed"):
            self.store.set_day(day, closed=True,
                               note=str(data.get("note", "")).strip())
        else:
            self.store.set_day(day, closed=False,
                               open_min=sched.parse_hhmm(data.get("open")),
                               close_min=sched.parse_hhmm(data.get("close")),
                               note=str(data.get("note", "")).strip())
        self.json({"ok": True, "info": self.store.day_info(day)})

    def api_admin_holiday(self, data):
        day = str(data.get("day", ""))
        if data.get("remove"):
            self.store.remove_holiday(day)
        else:
            self.store.add_holiday(day, str(data.get("name", "Public holiday")))
        self.json({"ok": True, "info": self.store.day_info(day)})

    def api_admin_service(self, data):
        fields = {}
        for key in ("name", "category", "note"):
            if key in data:
                fields[key] = str(data[key]).strip()
        for key in ("price", "duration_min", "active", "sort"):
            if key in data:
                try:
                    fields[key] = int(data[key])
                except (TypeError, ValueError):
                    raise BookingError(f"{key.replace('_', ' ')} has to be a number.")
        self.store.save_service(str(data.get("id", "")), **fields)
        self.json({"ok": True, "services": self.store.services(active_only=False)})

    def api_admin_new_service(self, data):
        name = str(data.get("name", "")).strip()
        if len(name) < 2:
            raise BookingError("Give the service a name.")
        try:
            price = int(data.get("price", 0))
            duration = int(data.get("duration_min", 0))
        except (TypeError, ValueError):
            raise BookingError("Price and duration have to be numbers.")
        if duration <= 0:
            raise BookingError("How many minutes does it take?")
        self.store.add_service(name, str(data.get("category", "Extras")).strip()
                               or "Extras", price, duration,
                               str(data.get("note", "")).strip())
        self.json({"ok": True, "services": self.store.services(active_only=False)})

    def api_admin_pin(self, data):
        pin = str(data.get("pin", "")).strip()
        if not pin.isdigit() or not (4 <= len(pin) <= 8):
            raise BookingError("The PIN has to be 4 to 8 digits.")
        self.store.set_setting("admin_pin", pin)
        self.json({"ok": True})

    def api_admin_settings(self, data):
        for key in ("lead_time_min", "slot_step_min", "horizon_days"):
            if key in data:
                try:
                    value = int(data[key])
                except (TypeError, ValueError):
                    raise BookingError(f"{key.replace('_', ' ')} has to be a number.")
                if key == "slot_step_min" and value not in (5, 10, 15, 20, 30):
                    raise BookingError("Slots step by 5, 10, 15, 20 or 30 minutes.")
                if value < 0:
                    raise BookingError("That cannot be negative.")
                self.store.set_setting(key, value)
        self.json({"ok": True, "settings": {
            key: self.store.setting_int(key)
            for key in ("lead_time_min", "slot_step_min", "horizon_days")}})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, quiet=False):
        self.store = store
        self.sessions = Sessions()
        self.quiet = quiet
        handler = type("BoundHandler", (Handler,),
                       {"store": store, "sessions": self.sessions})
        super().__init__(address, handler)

    def log(self, message):
        if not self.quiet:
            stamp = self.store.now().strftime("%H:%M:%S")
            print(f"{stamp}  {message}", flush=True)


def serve(db_path="barbershop.db", host="0.0.0.0", port=8080, quiet=False):
    store = Store(db_path)
    server = Server((host, port), store, quiet=quiet)
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"{SHOP['name']} booking - http://{shown}:{port}")
    print(f"  customers   http://{shown}:{port}/")
    print(f"  Jay's diary http://{shown}:{port}/admin   (PIN "
          f"{store.setting('admin_pin')})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        store.close()
    return 0
