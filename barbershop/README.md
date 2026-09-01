# Faded Studio by Jay — bookings

A booking system for a one-chair barbershop: customers pick a service, a
day and a time on their phone; Jay runs the day from a diary page. It is
plain Python — `sqlite3` for storage, `http.server` for the web — so
there is nothing to install beyond Python 3.11 or newer.

```
python -m barbershop serve            # http://localhost:8080
```

| Page | Who it is for |
| --- | --- |
| `/` | customers — the price list, the calendar, the booking form |
| `/admin` | Jay — the day's chair, walk-ins, time off, prices, takings |

On Windows, double-click `scripts/start_barbershop.bat` instead. Leave
the window open while the shop is trading.

The first run creates `barbershop.db` next to wherever you ran it, seeded
with the price list off the board and the South African public holidays
for this year and next. **The admin PIN starts as `1234` — change it**
(Settings → Change the PIN, or `python -m barbershop pin 4821`).

## What it does

**For customers**

- The whole price list, grouped the way the board is, with the price and
  how long the chair is held for.
- A month calendar. Closed days and fully-booked days are dim, so nobody
  picks a Monday by mistake.
- Only times that actually fit are offered: a 90-minute cut-and-colour
  stops being offered at 17:30 because the shop closes at 19:00.
- A reference (`FSJ-XXXXX`) to quote at the door, and a "find or cancel"
  box that takes the reference plus the mobile number the booking was
  made on.

**For Jay**

- Diary — every appointment for a day with the customer's number as a tap-to-call
  link, and buttons for done / no-show / move / cancel.
- Walk-ins and phone bookings, typed straight in at any time, even a
  service switched off for online booking.
- Blocked time for lunch or a supply run — the chair goes busy without a
  customer against it.
- Hours: close a single day, give a day its own hours, add a public
  holiday.
- Price list: change a price or a duration, switch a service off, add a
  new one. New bookings pick the change up immediately; bookings already
  in the diary keep the price they were made at.
- Takings for any date range, from the appointments marked done.
- Booking rules: how much notice online bookings need (30 minutes), how
  far apart slots start (15 minutes), how far ahead customers may book
  (60 days).

## Hours and durations

The board on the wall drives the defaults:

| | |
| --- | --- |
| Tuesday–Saturday | 9am – 7pm |
| Sunday and public holidays | 9am – 6pm |
| Monday | closed |

A public holiday runs Sunday hours even when it falls on a Monday; close
it from the diary if the shop is taking the day off instead.

Durations Jay gave are exact — haircut 30–45 minutes, shave 30, cut,
colour, wash and set 90, package one 1h15, package two 90. The chair is
held for the longer end of a range (a haircut books 45 minutes) so a slow
cut never runs into the next customer; a quick one just finishes early.
Every other duration on the list is a first estimate — change any of them
in Settings once real days show what they should be.

## Running it where customers can reach it

The server listens on every interface, so:

- **In the shop**, on the shop's wifi, customers reach it at
  `http://<the laptop's IP>:8080/`. Reserve the laptop's IP on the router
  so the address stops changing.
- **On the internet**, put it behind a reverse proxy that terminates
  HTTPS (Caddy, nginx, a Cloudflare tunnel) and point a domain at it. The
  admin PIN is the only lock on `/admin`, so do not expose it over plain
  HTTP.

Back up `barbershop.db` — it is the whole diary. Copying the file while
the server runs is safe.

## Command line

```
python -m barbershop serve [--host 0.0.0.0] [--port 8080]
python -m barbershop today [--ahead 2]     # print today's chair
python -m barbershop day 2026-09-08
python -m barbershop services [--all]      # print the price list
python -m barbershop pin 4821              # change the admin PIN
python -m barbershop --db /path/to.db ...  # use another database file
```

## Tests

```
python -m pytest tests/test_barbershop_schedule.py \
                 tests/test_barbershop_store.py \
                 tests/test_barbershop_web.py
```

The tests cover the slot arithmetic, the booking rules (closing time,
double bookings, blocked time, the notice period, cancellations) and the
web layer end to end, including that the diary stays behind the PIN.
