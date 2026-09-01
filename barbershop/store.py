"""SQLite storage - one file on disk holds the whole shop.

There is one chair, so the rule that matters is simple: two live bookings
may never overlap. `create_booking` checks that inside the same
transaction that writes the row, so two customers tapping "Book" on the
same slot at the same moment cannot both win.

The connection is shared across the web server's threads (writes are a
handful a day) and every call takes `self._lock`, so sqlite only ever sees
one statement at a time.
"""

import random
import sqlite3
import string
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import SHOP
from . import schedule as sched
from .services import seed_rows

LIVE = ("booked", "confirmed")
FINISHED = ("completed", "cancelled", "no_show")
ALL_STATUSES = LIVE + FINISHED

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
            self._seed()
            self.db.commit()

    # ---------------------------------------------------------------- setup

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
        return booking

    def create_booking(self, day, start_min, service_id, customer_name, phone,
                       notes="", source="online", admin=False):
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
        if not admin and not (first <= as_date <= last):
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
                    " notes, status, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?)",
                    (ref, day, start_min, duration, service["id"],
                     service["name"], service["price"], customer_name, phone,
                     str(notes or "").strip(), source,
                     self.now().isoformat(timespec="seconds")))
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
