"""SQLite storage - one file on disk holds the whole shop.

There is one chair, so the rule that matters is simple: two live bookings
may never overlap. `create_booking` checks that inside the same
transaction that writes the row, so two customers tapping "Book" on the
same slot at the same moment cannot both win.

Customers hold an account - a name, a cellphone number and a PIN - so the
portal can show them their own appointments and let them set up a repeat
(the same cut, same time, every two weeks). Their PIN is stored as a
salted PBKDF2 hash; the phone number is the account name.

The connection is shared across the web server's threads (writes are a
handful a day) and every call takes `self._lock`, so sqlite only ever sees
one statement at a time.
"""

import hashlib
import hmac
import os
import random
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import SHOP
from . import schedule as sched
from .services import seed_rows

LIVE = ("booked", "confirmed")
FINISHED = ("completed", "cancelled", "no_show")
ALL_STATUSES = LIVE + FINISHED

MAX_REPEATS = 12                 # a repeat books at most a year of a weekly cut
REPEAT_EVERY = (1, 2, 3, 4)      # weekly, fortnightly, every three, monthly
SESSION_DAYS = 60                # how long a customer stays signed in
PIN_ROUNDS = 200_000

DEFAULT_SETTINGS = {
    "admin_pin": "1234",
    "lead_time_min": "30",       # no online booking inside the next 30 minutes
    "slot_step_min": "15",       # slots start on the quarter hour
    "horizon_days": "60",        # how far ahead customers may book
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    price        INTEGER NOT NULL DEFAULT 0,
    duration_min INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    sort         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ref           TEXT NOT NULL UNIQUE,
    day           TEXT NOT NULL,
    start_min     INTEGER NOT NULL,
    duration_min  INTEGER NOT NULL,
    service_id    TEXT NOT NULL,
    service_name  TEXT NOT NULL,
    price         INTEGER NOT NULL DEFAULT 0,
    customer_name TEXT NOT NULL,
    phone         TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'booked',
    source        TEXT NOT NULL DEFAULT 'online',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_day ON bookings (day, start_min);
CREATE TABLE IF NOT EXISTS blocks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    day       TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS blocks_day ON blocks (day);
CREATE TABLE IF NOT EXISTS day_overrides (
    day       TEXT PRIMARY KEY,
    closed    INTEGER NOT NULL DEFAULT 0,
    open_min  INTEGER,
    close_min INTEGER,
    note      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS holidays (
    day  TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL UNIQUE,
    pin_hash   TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS client_sessions (
    token       TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    service_id  TEXT NOT NULL,
    first_day   TEXT NOT NULL,
    start_min   INTEGER NOT NULL,
    every_weeks INTEGER NOT NULL,
    times       INTEGER NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL
);
"""


class BookingError(Exception):
    """Something the customer or Jay needs to be told about, in plain words."""


def normalise_phone(raw: str) -> str:
    """Keep the digits (and a leading +) so 062 541 0305 matches 0625410305."""
    text = str(raw or "").strip()
    keep = [c for c in text if c.isdigit()]
    if text.startswith("+"):
        return "+" + "".join(keep)
    return "".join(keep)


def hash_pin(pin: str) -> str:
    """Salted PBKDF2 - a four digit PIN is short, so never store it plainly."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt, PIN_ROUNDS)
    return f"pbkdf2${PIN_ROUNDS}${salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = str(stored).split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", str(pin).encode(),
                                        bytes.fromhex(salt), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def check_pin_format(pin: str) -> str:
    pin = str(pin or "").strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        raise BookingError("Your PIN has to be 4 to 8 numbers.")
    return pin


def make_ref() -> str:
    """Booking reference the customer quotes at the door, e.g. FSJ-7K2QM."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I/O/0/1 to misread
    return "FSJ-" + "".join(random.choice(alphabet) for _ in range(5))


class Store:
    def __init__(self, path="barbershop.db"):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.tz = ZoneInfo(SHOP["timezone"])
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self._seed()
            self.db.commit()

    # ---------------------------------------------------------------- setup

    def _migrate(self):
        """Add columns a database made by an older version has not got."""
        columns = {row["name"] for row in
                   self.db.execute("PRAGMA table_info(bookings)")}
        if "customer_id" not in columns:
            self.db.execute("ALTER TABLE bookings ADD COLUMN customer_id INTEGER")
        if "series_id" not in columns:
            self.db.execute("ALTER TABLE bookings ADD COLUMN series_id INTEGER")

    def _seed(self):
        have = self.db.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        if not have:
            self.db.executemany(
                "INSERT INTO services (id, name, category, price, duration_min,"
                " note, active, sort) VALUES (:id, :name, :category, :price,"
                " :duration_min, :note, :active, :sort)", seed_rows())
        for key, value in DEFAULT_SETTINGS.items():
            self.db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value))
        this_year = self.today().year
        for year in (this_year, this_year + 1):
            for day, name in sched.sa_public_holidays(year).items():
                self.db.execute(
                    "INSERT OR IGNORE INTO holidays (day, name) VALUES (?, ?)",
                    (day, name))

    def close(self):
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------- settings

    def setting(self, key, default=None):
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def setting_int(self, key, default=0):
        try:
            return int(self.setting(key, default))
        except (TypeError, ValueError):
            return default

    def set_setting(self, key, value):
        with self._lock:
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self.db.commit()

    # ------------------------------------------------------------ shop time

    def now(self) -> datetime:
        """Wall-clock time in the shop, not on the server."""
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def now_minutes(self) -> int:
        moment = self.now()
        return moment.hour * 60 + moment.minute

    # ------------------------------------------------------------- services

    def services(self, active_only=True):
        query = "SELECT * FROM services"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY sort, name"
        with self._lock:
            return [dict(r) for r in self.db.execute(query)]

    def service(self, service_id):
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        return dict(row) if row else None

    def save_service(self, service_id, **fields):
        allowed = ("name", "category", "price", "duration_min", "note",
                   "active", "sort")
        sets, values = [], []
        for key in allowed:
            if key in fields:
                sets.append(f"{key} = ?")
                values.append(fields[key])
        if not sets:
            return
        if "duration_min" in fields and int(fields["duration_min"]) <= 0:
            raise BookingError("A service needs a duration of at least a minute.")
        values.append(service_id)
        with self._lock:
            self.db.execute(
                f"UPDATE services SET {', '.join(sets)} WHERE id = ?", values)
            self.db.commit()

    def add_service(self, name, category, price, duration_min, note=""):
        base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
        base = base or "service"
        service_id, n = base, 2
        with self._lock:
            while self.db.execute("SELECT 1 FROM services WHERE id = ?",
                                  (service_id,)).fetchone():
                service_id, n = f"{base}-{n}", n + 1
            top = self.db.execute("SELECT MAX(sort) FROM services").fetchone()[0] or 0
            self.db.execute(
                "INSERT INTO services (id, name, category, price, duration_min,"
                " note, active, sort) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (service_id, name, category, int(price), int(duration_min),
                 note, top + 1))
            self.db.commit()
        return service_id

    # ---------------------------------------------------------------- hours

    def holidays(self):
        with self._lock:
            return {r["day"]: r["name"]
                    for r in self.db.execute("SELECT * FROM holidays ORDER BY day")}

    def add_holiday(self, day, name):
        with self._lock:
            self.db.execute(
                "INSERT INTO holidays (day, name) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET name = excluded.name",
                (day, name))
            self.db.commit()

    def remove_holiday(self, day):
        with self._lock:
            self.db.execute("DELETE FROM holidays WHERE day = ?", (day,))
            self.db.commit()

    def set_day(self, day, closed=False, open_min=None, close_min=None, note=""):
        """Close a single day, or give it its own hours."""
        if not closed and open_min is not None and close_min is not None:
            if close_min <= open_min:
                raise BookingError("Closing time has to be after opening time.")
        with self._lock:
            self.db.execute(
                "INSERT INTO day_overrides (day, closed, open_min, close_min, note)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET"
                " closed = excluded.closed, open_min = excluded.open_min,"
                " close_min = excluded.close_min, note = excluded.note",
                (day, 1 if closed else 0, open_min, close_min, note))
            self.db.commit()

    def clear_day(self, day):
        with self._lock:
            self.db.execute("DELETE FROM day_overrides WHERE day = ?", (day,))
            self.db.commit()

    def day_info(self, day) -> dict:
        """Hours and why: normal week, public holiday, or Jay closed it."""
        day = str(day)
        with self._lock:
            override = self.db.execute(
                "SELECT * FROM day_overrides WHERE day = ?", (day,)).fetchone()
            holiday = self.db.execute(
                "SELECT name FROM holidays WHERE day = ?", (day,)).fetchone()
        as_date = date.fromisoformat(day)
        info = {
            "day": day,
            "weekday": as_date.strftime("%A"),
            "label": as_date.strftime("%a %d %b %Y"),
            "holiday": holiday["name"] if holiday else None,
            "note": override["note"] if override else "",
            "custom": bool(override),
        }
        if override and override["closed"]:
            info.update(open_min=None, close_min=None, closed=True,
                        reason=override["note"] or "Closed")
            return info
        if override and override["open_min"] is not None:
            info.update(open_min=override["open_min"],
                        close_min=override["close_min"], closed=False,
                        reason=override["note"] or "Special hours")
            return info
        hours = sched.default_hours_for(as_date, is_holiday=bool(holiday))
        if hours is None:
            info.update(open_min=None, close_min=None, closed=True,
                        reason=f"Closed on {as_date.strftime('%A')}s")
            return info
        info.update(open_min=hours[0], close_min=hours[1], closed=False,
                    reason=holiday["name"] if holiday else "")
        return info

    # --------------------------------------------------------------- blocks

    def blocks(self, day):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM blocks WHERE day = ? ORDER BY start_min", (str(day),))]

    def add_block(self, day, start_min, end_min, reason=""):
        if end_min <= start_min:
            raise BookingError("The end of a break has to be after its start.")
        clash = [b for b in self.bookings_for_day(day, live_only=True)
                 if sched.overlaps(start_min, end_min,
                                   b["start_min"], b["end_min"])]
        if clash:
            names = ", ".join(f"{b['time']} {b['customer_name']}" for b in clash)
            raise BookingError(f"That time is booked already: {names}.")
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO blocks (day, start_min, end_min, reason)"
                " VALUES (?, ?, ?, ?)", (str(day), start_min, end_min, reason))
            self.db.commit()
        return cur.lastrowid

    def remove_block(self, block_id):
        with self._lock:
            self.db.execute("DELETE FROM blocks WHERE id = ?", (int(block_id),))
            self.db.commit()

    # ------------------------------------------------------------ the chair

    def busy_intervals(self, day):
        """Everything holding the chair on `day`: live bookings and breaks."""
        day = str(day)
        with self._lock:
            rows = self.db.execute(
                "SELECT start_min, start_min + duration_min AS end_min"
                " FROM bookings WHERE day = ? AND status IN (?, ?)",
                (day, *LIVE)).fetchall()
            breaks = self.db.execute(
                "SELECT start_min, end_min FROM blocks WHERE day = ?",
                (day,)).fetchall()
        return [(r["start_min"], r["end_min"]) for r in rows] + \
               [(r["start_min"], r["end_min"]) for r in breaks]

    def earliest_start(self, day) -> int:
        """First minute a customer may still book into on `day`."""
        day = str(day)
        today = self.today().isoformat()
        if day < today:
            return 24 * 60          # the past is never bookable
        if day > today:
            return 0
        return self.now_minutes() + self.setting_int("lead_time_min", 30)

    def available(self, day, duration_min, admin=False):
        """Start times that fit a `duration_min` service on `day`."""
        info = self.day_info(day)
        if info["closed"]:
            return []
        earliest = 0 if admin else self.earliest_start(day)
        return sched.slot_starts(
            info["open_min"], info["close_min"], self.busy_intervals(day),
            int(duration_min), step=self.setting_int("slot_step_min", 15),
            earliest=earliest)

    def horizon(self):
        """The window customers may book inside, as (first_day, last_day)."""
        first = self.today()
        last = first + timedelta(days=self.setting_int("horizon_days", 60))
        return first, last

    # ------------------------------------------------------------- bookings

    def _row(self, row):
        booking = dict(row)
        booking["end_min"] = booking["start_min"] + booking["duration_min"]
        booking["time"] = sched.friendly(booking["start_min"])
        booking["end_time"] = sched.friendly(booking["end_min"])
        booking["date_label"] = date.fromisoformat(
            booking["day"]).strftime("%a %d %b %Y")
        booking["repeat"] = bool(booking.get("series_id"))
        booking["live"] = booking["status"] in LIVE
        return booking

    def create_booking(self, day, start_min, service_id, customer_name, phone,
                       notes="", source="online", admin=False,
                       customer_id=None, series_id=None, ignore_horizon=False):
        service = self.service(service_id)
        if not service:
            raise BookingError("That service is not on the list any more.")
        if not admin and not service["active"]:
            raise BookingError(f"{service['name']} is not being booked online.")
        customer_name = str(customer_name or "").strip()
        if len(customer_name) < 2:
            raise BookingError("Please give the name the booking is under.")
        phone = normalise_phone(phone)
        if not admin and len(phone) < 9:
            raise BookingError("Please give a mobile number Jay can reach you on.")

        day = str(day)
        start_min = int(start_min)
        duration = int(service["duration_min"])
        end_min = start_min + duration

        try:
            as_date = date.fromisoformat(day)
        except ValueError:
            raise BookingError("That is not a valid date.")
        first, last = self.horizon()
        if not admin and not ignore_horizon and not (first <= as_date <= last):
            raise BookingError(
                f"Bookings open from {first.strftime('%d %b')} to "
                f"{last.strftime('%d %b')}.")

        info = self.day_info(day)
        if info["closed"]:
            raise BookingError(f"The shop is closed on {info['label']}.")
        if start_min < info["open_min"] or end_min > info["close_min"]:
            raise BookingError(
                f"{service['name']} takes {duration} minutes, so it has to "
                f"start between {sched.friendly(info['open_min'])} and "
                f"{sched.friendly(info['close_min'] - duration)}.")
        if not admin and start_min < self.earliest_start(day):
            raise BookingError("That time has passed - please pick a later slot.")

        with self._lock:
            # BEGIN IMMEDIATE takes the write lock before we check, so the
            # overlap test and the insert cannot be split by another booking.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                clash = self.db.execute(
                    "SELECT 1 FROM bookings WHERE day = ? AND status IN (?, ?)"
                    " AND start_min < ? AND start_min + duration_min > ?",
                    (day, *LIVE, end_min, start_min)).fetchone()
                blocked = self.db.execute(
                    "SELECT 1 FROM blocks WHERE day = ? AND start_min < ?"
                    " AND end_min > ?", (day, end_min, start_min)).fetchone()
                if clash or blocked:
                    raise BookingError(
                        "Sorry - that slot was taken while you were choosing. "
                        "Please pick another time.")
                for _ in range(20):
                    ref = make_ref()
                    if not self.db.execute("SELECT 1 FROM bookings WHERE ref = ?",
                                           (ref,)).fetchone():
                        break
                else:
                    raise BookingError("Could not raise a booking reference.")
                cur = self.db.execute(
                    "INSERT INTO bookings (ref, day, start_min, duration_min,"
                    " service_id, service_name, price, customer_name, phone,"
                    " notes, status, source, created_at, customer_id, series_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?, ?)",
                    (ref, day, start_min, duration, service["id"],
                     service["name"], service["price"], customer_name, phone,
                     str(notes or "").strip(), source,
                     self.now().isoformat(timespec="seconds"),
                     customer_id, series_id))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            row = self.db.execute("SELECT * FROM bookings WHERE id = ?",
                                  (cur.lastrowid,)).fetchone()
        return self._row(row)

    def booking(self, booking_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM bookings WHERE id = ?",
                                  (int(booking_id),)).fetchone()
        return self._row(row) if row else None

    def by_ref(self, ref, phone=None):
        """Look a booking up the way a customer has it: reference, and their
        number when it is the customer asking."""
        ref = str(ref or "").strip().upper()
        if ref and not ref.startswith("FSJ-"):
            ref = "FSJ-" + ref
        with self._lock:
            row = self.db.execute("SELECT * FROM bookings WHERE ref = ?",
                                  (ref,)).fetchone()
        if not row:
            return None
        if phone is not None and normalise_phone(phone) != row["phone"]:
            return None
        return self._row(row)

    def bookings_for_day(self, day, live_only=False):
        query = "SELECT * FROM bookings WHERE day = ?"
        args = [str(day)]
        if live_only:
            query += " AND status IN (?, ?)"
            args += list(LIVE)
        query += " ORDER BY start_min"
        with self._lock:
            return [self._row(r) for r in self.db.execute(query, args)]

    def upcoming(self, days=14, limit=200):
        first = self.today()
        last = first + timedelta(days=days)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bookings WHERE day BETWEEN ? AND ?"
                " AND status IN (?, ?) ORDER BY day, start_min LIMIT ?",
                (first.isoformat(), last.isoformat(), *LIVE, limit)).fetchall()
        return [self._row(r) for r in rows]

    def history_for_phone(self, phone, limit=20):
        phone = normalise_phone(phone)
        if not phone:
            return []
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bookings WHERE phone = ?"
                " ORDER BY day DESC, start_min DESC LIMIT ?",
                (phone, limit)).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, booking_id, status):
        if status not in ALL_STATUSES:
            raise BookingError(f"Unknown status {status!r}.")
        with self._lock:
            cur = self.db.execute("UPDATE bookings SET status = ? WHERE id = ?",
                                  (status, int(booking_id)))
            self.db.commit()
        if not cur.rowcount:
            raise BookingError("That booking is not in the diary any more.")
        return self.booking(booking_id)

    def cancel_by_ref(self, ref, phone=None):
        booking = self.by_ref(ref, phone)
        if not booking:
            raise BookingError(
                "No booking matches that reference and number.")
        if booking["status"] not in LIVE:
            raise BookingError(f"That booking is already {booking['status']}.")
        today = self.today().isoformat()
        if (booking["day"], booking["start_min"]) < (today, self.now_minutes()):
            raise BookingError(
                "That appointment has already started - please phone the shop.")
        return self.set_status(booking["id"], "cancelled")

    def move_booking(self, booking_id, day, start_min):
        """Reschedule, checking the new slot the same way a new booking is."""
        booking = self.booking(booking_id)
        if not booking:
            raise BookingError("That booking is not in the diary any more.")
        day, start_min = str(day), int(start_min)
        end_min = start_min + booking["duration_min"]
        info = self.day_info(day)
        if info["closed"]:
            raise BookingError(f"The shop is closed on {info['label']}.")
        if start_min < info["open_min"] or end_min > info["close_min"]:
            raise BookingError("That is outside the shop's hours for that day.")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                clash = self.db.execute(
                    "SELECT 1 FROM bookings WHERE day = ? AND id != ?"
                    " AND status IN (?, ?) AND start_min < ?"
                    " AND start_min + duration_min > ?",
                    (day, int(booking_id), *LIVE, end_min, start_min)).fetchone()
                blocked = self.db.execute(
                    "SELECT 1 FROM blocks WHERE day = ? AND start_min < ?"
                    " AND end_min > ?", (day, end_min, start_min)).fetchone()
                if clash or blocked:
                    raise BookingError("Something else is in the chair then.")
                self.db.execute(
                    "UPDATE bookings SET day = ?, start_min = ? WHERE id = ?",
                    (day, start_min, int(booking_id)))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.booking(booking_id)

    # ---------------------------------------------------------------- clients

    def register(self, name, phone, pin):
        """Open an account. The cellphone number is the account name."""
        name = str(name or "").strip()
        if len(name) < 2:
            raise BookingError("Please give us your name.")
        phone = normalise_phone(phone)
        if len(phone) < 9:
            raise BookingError("Please give the cellphone number you use.")
        pin = check_pin_format(pin)
        with self._lock:
            if self.db.execute("SELECT 1 FROM customers WHERE phone = ?",
                               (phone,)).fetchone():
                raise BookingError(
                    "That number already has an account - sign in instead, "
                    "or ask Jay to reset your PIN.")
            cur = self.db.execute(
                "INSERT INTO customers (name, phone, pin_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (name, phone, hash_pin(pin),
                 self.now().isoformat(timespec="seconds")))
            self.db.commit()
        return self.customer(cur.lastrowid)

    def sign_in(self, phone, pin):
        """The account for this number and PIN, or None."""
        row = self.customer_by_phone(phone)
        if not row:
            return None
        with self._lock:
            stored = self.db.execute(
                "SELECT pin_hash FROM customers WHERE id = ?",
                (row["id"],)).fetchone()["pin_hash"]
        return row if check_pin(str(pin or ""), stored) else None

    def customer(self, customer_id):
        with self._lock:
            row = self.db.execute(
                "SELECT id, name, phone, notes, created_at FROM customers"
                " WHERE id = ?", (int(customer_id),)).fetchone()
        return dict(row) if row else None

    def customer_by_phone(self, phone):
        with self._lock:
            row = self.db.execute(
                "SELECT id, name, phone, notes, created_at FROM customers"
                " WHERE phone = ?", (normalise_phone(phone),)).fetchone()
        return dict(row) if row else None

    def update_customer(self, customer_id, name=None, pin=None, notes=None):
        sets, values = [], []
        if name is not None:
            name = str(name).strip()
            if len(name) < 2:
                raise BookingError("Please give us your name.")
            sets.append("name = ?")
            values.append(name)
        if pin is not None:
            sets.append("pin_hash = ?")
            values.append(hash_pin(check_pin_format(pin)))
        if notes is not None:
            sets.append("notes = ?")
            values.append(str(notes).strip())
        if not sets:
            return self.customer(customer_id)
        values.append(int(customer_id))
        with self._lock:
            self.db.execute(
                f"UPDATE customers SET {', '.join(sets)} WHERE id = ?", values)
            self.db.commit()
        return self.customer(customer_id)

    def customers(self, search="", limit=200):
        """Everyone with an account, newest first, for Jay's client list."""
        search = str(search or "").strip()
        query = ("SELECT c.id, c.name, c.phone, c.notes, c.created_at,"
                 " (SELECT COUNT(*) FROM bookings b WHERE b.customer_id = c.id"
                 "  AND b.status = 'completed') AS visits,"
                 " (SELECT COUNT(*) FROM bookings b WHERE b.customer_id = c.id"
                 "  AND b.status = 'no_show') AS no_shows,"
                 " (SELECT COUNT(*) FROM bookings b WHERE b.customer_id = c.id"
                 "  AND b.status IN ('booked', 'confirmed') AND b.day >= ?)"
                 "  AS upcoming FROM customers c")
        args = [self.today().isoformat()]
        if search:
            query += " WHERE c.name LIKE ? OR c.phone LIKE ?"
            args += [f"%{search}%", f"%{normalise_phone(search) or search}%"]
        query += " ORDER BY c.id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self.db.execute(query, args)]

    # -------------------------------------------------------------- sessions

    def start_session(self, customer_id):
        """Sign a customer in for SESSION_DAYS and hand back their token."""
        token = secrets.token_urlsafe(24)
        expires = self.now() + timedelta(days=SESSION_DAYS)
        with self._lock:
            self.db.execute(
                "INSERT INTO client_sessions (token, customer_id, expires_at)"
                " VALUES (?, ?, ?)",
                (token, int(customer_id), expires.isoformat(timespec="seconds")))
            self.db.execute("DELETE FROM client_sessions WHERE expires_at < ?",
                            (self.now().isoformat(timespec="seconds"),))
            self.db.commit()
        return token

    def session_customer(self, token):
        """The account behind a session cookie, or None."""
        if not token:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT customer_id, expires_at FROM client_sessions"
                " WHERE token = ?", (str(token),)).fetchone()
        if not row:
            return None
        if row["expires_at"] < self.now().isoformat(timespec="seconds"):
            self.end_session(token)
            return None
        return self.customer(row["customer_id"])

    def end_session(self, token):
        with self._lock:
            self.db.execute("DELETE FROM client_sessions WHERE token = ?",
                            (str(token or ""),))
            self.db.commit()

    def end_all_sessions(self, customer_id):
        """Used when a PIN changes - every other phone gets signed out."""
        with self._lock:
            self.db.execute("DELETE FROM client_sessions WHERE customer_id = ?",
                            (int(customer_id),))
            self.db.commit()

    # ------------------------------------------------- a customer's own diary

    def customer_bookings(self, customer_id, upcoming=True, limit=100):
        today = self.today().isoformat()
        if upcoming:
            query = ("SELECT * FROM bookings WHERE customer_id = ?"
                     " AND status IN (?, ?) AND day >= ?"
                     " ORDER BY day, start_min LIMIT ?")
            args = (int(customer_id), *LIVE, today, int(limit))
        else:
            query = ("SELECT * FROM bookings WHERE customer_id = ?"
                     " AND (day < ? OR status NOT IN (?, ?))"
                     " ORDER BY day DESC, start_min DESC LIMIT ?")
            args = (int(customer_id), today, *LIVE, int(limit))
        with self._lock:
            return [self._row(r) for r in self.db.execute(query, args)]

    def owned_booking(self, customer_id, booking_id):
        booking = self.booking(booking_id)
        if not booking or booking["customer_id"] != int(customer_id):
            raise BookingError("That booking is not on your account.")
        return booking

    def cancel_own(self, customer_id, booking_id):
        """A customer calling off one of their own appointments."""
        booking = self.owned_booking(customer_id, booking_id)
        if booking["status"] not in LIVE:
            raise BookingError(f"That booking is already {booking['status']}.")
        if (booking["day"], booking["start_min"]) < \
                (self.today().isoformat(), self.now_minutes()):
            raise BookingError(
                "That appointment has already started - please phone the shop.")
        return self.set_status(booking["id"], "cancelled")

    # --------------------------------------------------------------- repeats

    def repeat_dates(self, first_day, every_weeks, times):
        """The dates of a repeat: same weekday, every so many weeks."""
        every_weeks, times = int(every_weeks), int(times)
        if every_weeks not in REPEAT_EVERY:
            raise BookingError("A repeat runs every 1, 2, 3 or 4 weeks.")
        if not (2 <= times <= MAX_REPEATS):
            raise BookingError(f"A repeat runs 2 to {MAX_REPEATS} times.")
        try:
            start = date.fromisoformat(str(first_day))
        except ValueError:
            raise BookingError("That is not a valid date.")
        return [(start + timedelta(weeks=every_weeks * n)).isoformat()
                for n in range(times)]

    def plan_repeat(self, service_id, first_day, start_min, every_weeks, times):
        """What a repeat would look like, before anything is written.

        Every date is checked the way a single booking is - the shop has to
        be open, the service has to finish before closing, and the chair has
        to be free - so the customer sees which dates are theirs and which
        ones they will have to arrange another way.
        """
        service = self.service(service_id)
        if not service or not service["active"]:
            raise BookingError("That service is not being booked online.")
        start_min = int(start_min)
        duration = int(service["duration_min"])
        end_min = start_min + duration
        plan = []
        for day in self.repeat_dates(first_day, every_weeks, times):
            info = self.day_info(day)
            entry = {"day": day, "date_label": info["label"], "ok": False,
                     "time": sched.friendly(start_min), "reason": ""}
            if info["closed"]:
                entry["reason"] = f"closed ({info['reason'].lower()})"
            elif start_min < info["open_min"] or end_min > info["close_min"]:
                entry["reason"] = "outside the shop's hours that day"
            elif start_min < self.earliest_start(day):
                entry["reason"] = "too soon"
            elif any(sched.overlaps(start_min, end_min, busy_start, busy_end)
                     for busy_start, busy_end in self.busy_intervals(day)):
                entry["reason"] = "already taken"
            else:
                entry["ok"] = True
            plan.append(entry)
        return {"service": service, "plan": plan,
                "free": sum(1 for entry in plan if entry["ok"]),
                "start_min": start_min, "time": sched.friendly(start_min),
                "every_weeks": int(every_weeks), "times": int(times)}

    def create_repeat(self, customer_id, service_id, first_day, start_min,
                      every_weeks, times, notes=""):
        """Book the dates of a repeat that are free, and say which were not."""
        customer = self.customer(customer_id)
        if not customer:
            raise BookingError("Please sign in first.")
        preview = self.plan_repeat(service_id, first_day, start_min,
                                   every_weeks, times)
        if not preview["free"]:
            raise BookingError(
                "None of those dates are free - try another time or day.")
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO series (customer_id, service_id, first_day,"
                " start_min, every_weeks, times, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(customer_id), preview["service"]["id"], str(first_day),
                 int(start_min), int(every_weeks), int(times),
                 str(notes or "").strip(),
                 self.now().isoformat(timespec="seconds")))
            self.db.commit()
            series_id = cur.lastrowid

        booked, missed = [], []
        for entry in preview["plan"]:
            if not entry["ok"]:
                missed.append(entry)
                continue
            try:
                booked.append(self.create_booking(
                    entry["day"], start_min, preview["service"]["id"],
                    customer["name"], customer["phone"], notes=notes,
                    source="repeat", customer_id=customer["id"],
                    series_id=series_id, ignore_horizon=True))
            except BookingError as exc:     # taken in the seconds since the plan
                missed.append(dict(entry, ok=False, reason=str(exc)))
        if not booked:
            with self._lock:
                self.db.execute("DELETE FROM series WHERE id = ?", (series_id,))
                self.db.commit()
            raise BookingError(
                "Those dates went while you were booking - please try again.")
        return {"series": self.series(series_id), "booked": booked,
                "missed": missed}

    def series(self, series_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM series WHERE id = ?",
                                  (int(series_id),)).fetchone()
        if not row:
            return None
        series = dict(row)
        service = self.service(series["service_id"])
        series["service_name"] = service["name"] if service else series["service_id"]
        series["time"] = sched.friendly(series["start_min"])
        series["weekday"] = date.fromisoformat(series["first_day"]).strftime("%A")
        series["every"] = ("every week" if series["every_weeks"] == 1
                           else f"every {series['every_weeks']} weeks")
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bookings WHERE series_id = ? ORDER BY day",
                (series["id"],)).fetchall()
        series["bookings"] = [self._row(r) for r in rows]
        today = self.today().isoformat()
        series["remaining"] = [b for b in series["bookings"]
                               if b["live"] and b["day"] >= today]
        return series

    def series_for_customer(self, customer_id, active_only=True):
        query = "SELECT id FROM series WHERE customer_id = ?"
        args = [int(customer_id)]
        if active_only:
            query += " AND status = 'active'"
        query += " ORDER BY id DESC"
        with self._lock:
            ids = [r["id"] for r in self.db.execute(query, args)]
        return [self.series(series_id) for series_id in ids]

    def cancel_series(self, series_id, customer_id=None):
        """Call off the rest of a repeat, leaving anything already done alone."""
        series = self.series(series_id)
        if not series or (customer_id is not None and
                          series["customer_id"] != int(customer_id)):
            raise BookingError("That repeat is not on your account.")
        cancelled = 0
        for booking in series["remaining"]:
            self.set_status(booking["id"], "cancelled")
            cancelled += 1
        with self._lock:
            self.db.execute("UPDATE series SET status = 'cancelled' WHERE id = ?",
                            (series["id"],))
            self.db.commit()
        return {"series": self.series(series["id"]), "cancelled": cancelled}

    # ---------------------------------------------------------------- takings

    def takings(self, first, last):
        """What was earned between two dates, from completed appointments."""
        with self._lock:
            rows = self.db.execute(
                "SELECT day, COUNT(*) AS jobs, SUM(price) AS rand"
                " FROM bookings WHERE day BETWEEN ? AND ? AND status = 'completed'"
                " GROUP BY day ORDER BY day", (str(first), str(last))).fetchall()
            top = self.db.execute(
                "SELECT service_name, COUNT(*) AS jobs, SUM(price) AS rand"
                " FROM bookings WHERE day BETWEEN ? AND ? AND status = 'completed'"
                " GROUP BY service_name ORDER BY rand DESC, jobs DESC LIMIT 10",
                (str(first), str(last))).fetchall()
            counts = self.db.execute(
                "SELECT status, COUNT(*) AS n FROM bookings"
                " WHERE day BETWEEN ? AND ? GROUP BY status",
                (str(first), str(last))).fetchall()
        days = [dict(r) for r in rows]
        return {
            "first": str(first),
            "last": str(last),
            "days": days,
            "services": [dict(r) for r in top],
            "statuses": {r["status"]: r["n"] for r in counts},
            "jobs": sum(d["jobs"] for d in days),
            "rand": sum(d["rand"] or 0 for d in days),
        }
