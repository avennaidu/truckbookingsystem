# Faded Studio by Jay — bookings

A booking system for a one-chair barbershop. Customers register with
their name and cellphone number, sign in on their phone, and book a
service — once, or standing every couple of weeks. Jay runs the day from
a diary page. It is plain Python — `sqlite3` for storage, `http.server`
for the web — so there is nothing to install beyond Python 3.11 or newer.

```
python -m barbershop serve            # http://localhost:8080
```

| Page | Who it is for |
| --- | --- |
| `/` | customers — sign in, the price list, the calendar, their own appointments |
| `/admin` | Jay — the day's chair, walk-ins, time off, prices, takings |

On Windows, double-click `scripts/start_barbershop.bat` instead. Leave
the window open while the shop is trading.

The first run creates `barbershop.db` next to wherever you ran it, seeded
with the price list off the board and the South African public holidays
for this year and next. **The admin PIN starts as `1234` — change it**
(Settings → Change the PIN, or `python -m barbershop pin 4821`).

## What it does

**For customers**

- An account, opened with a name, a cellphone number and a PIN of their
  choosing. The number is the account name, so there is nothing to
  remember. Signing in lasts two months on that phone.
- The whole price list, grouped the way the board is, with the price and
  how long the chair is held for. It shows before signing in, so people
  can check prices without registering; booking needs an account.
- **Several services in one sitting** — tap a cut, a beard trim and a hot
  towel and the chair is held for all three back to back, at what they
  add up to. Up to six; the same service twice is a father and son in one
  slot.
- A month calendar. Closed days and fully-booked days are dim, so nobody
  picks a Monday by mistake.
- Only times that actually fit are offered: a 90-minute cut-and-colour
  stops being offered at 17:30 because the shop closes at 19:00.
- **A quick extra, offered once.** After the services are chosen and
  before a day is picked, the page asks whether to add a nose or ear wax
  at 10% off. See below.
- **Standing appointments** — the same cut, the same day of the week, the
  same time, every 1 to 4 weeks, 2 to 12 times over. See below.
- "My appointments": everything coming up with its reference
  (`FSJ-XXXXX`) to quote at the door, cancel on each one, past visits
  underneath, and their own name and PIN to change.

**For Jay**

- Diary — every appointment for a day with the customer's number as a tap-to-call
  link, and buttons for done / no-show / move / cancel.
- Walk-ins and phone bookings, typed straight in at any time, with as
  many services as the sitting takes, and even a service switched off for
  online booking.
- Blocked time for lunch or a supply run — the chair goes busy without a
  customer against it.
- Hours: close a single day, give a day its own hours, add a public
  holiday.
- Price list: change a price or a duration, switch a service off, tick
  which services get offered as an extra, add a new one. New bookings pick the change up immediately; bookings already
  in the diary keep the price they were made at.
- Clients — everyone who has registered, with their number as a
  tap-to-call link, how many visits and no-shows they have, and a PIN
  reset for whoever has forgotten theirs.
- Takings for any date range, from the appointments marked done.
- Booking rules: how much notice online bookings need (30 minutes), how
  far apart slots start (15 minutes), how far ahead customers may book
  (60 days), and what comes off an extra taken at booking time (10%).

## The extra Jay would have asked about anyway

A wax on the end of a haircut costs ten minutes Jay has the customer for
already, so the page asks for it — once, after the services are chosen
and **before the day is picked**, which is what keeps the arithmetic
honest: those ten minutes are in the sitting before any time is offered,
so nothing gets added at the last moment that no longer fits.

Out of the box the extras are the nose wax and the ear wax at 10% off
(R50 becomes R45). Both parts are Jay's to change: tick **Extra** against
any service in the price list to have it offered, and set the discount in
Settings. The discount only ever comes off the extra, never off the
services, and half a rand rounds up.

The booking keeps what was added and what it saved, so the diary shows
`Haircut + Nose Wax` at R145 and the takings are the real takings. An
extra already in the sitting is never offered back, and a standing
appointment repeats the extra with every visit.

## Standing appointments

A customer picks the services, the first date and the time, then chooses
how often (every week, or every 2, 3 or 4 weeks) and how many times (2 to
12). Before anything is written the portal shows every date it would
take, with the ones that are not free marked and why — closed that day,
outside the hours, already taken.

Confirming books the dates that are free and says plainly which ones were
not, so the customer can arrange those separately rather than believing a
booking exists that does not. A standing run reaches past the 60-day
window a single booking may be made in — twelve fortnightly cuts is most
of a year — which is the point of holding the slot.

"Stop this repeat" cancels the appointments still to come and leaves
anything already done alone. Individual dates in the run can be cancelled
on their own, and Jay can move any one of them from the diary like any
other appointment.

## One sitting, several services

A booking holds one run of the chair. Pick more than one service and the
run is their durations added up and the price is their prices added up —
a haircut and a shave is 75 minutes at R180, and the times offered are
only the ones where a 75 minute run actually fits before closing.

The appointment reads as `Haircut + Shave` in the diary and on the
customer's phone, and it keeps the list of services on it, so changing a
price later never rewrites what somebody already booked.

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

## Accounts and the PIN

Customer PINs are 4 to 8 digits, stored as salted PBKDF2-SHA256 hashes,
never in the clear. A short PIN is easy to guess at, so sign-ins are
rationed: six wrong tries on one number and that number is shut out for
ten minutes. Changing a PIN signs every other phone out. Jay resets a
forgotten PIN from the Clients tab, which also signs that customer out
everywhere.

There is no SMS in this build, so nothing is sent to verify a number and
a forgotten PIN goes through Jay. If the shop later pays for an SMS
gateway, a one-time code fits in the same sign-in route.

## Running it where customers can reach it

The server listens on every interface, so:

- **In the shop**, on the shop's wifi, customers reach it at
  `http://<the laptop's IP>:8080/`. Reserve the laptop's IP on the router
  so the address stops changing.
- **On the internet**, put it behind a reverse proxy that terminates
  HTTPS (Caddy, nginx, a Cloudflare tunnel) and point a domain at it.
  Customer PINs and session cookies cross the wire on every sign-in, and
  the shop PIN is the only lock on `/admin`, so it must not be reachable
  over plain HTTP.

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
                 tests/test_barbershop_clients.py \
                 tests/test_barbershop_web.py
```

The tests cover the slot arithmetic, the booking rules (closing time,
double bookings, blocked time, the notice period, cancellations),
accounts and standing appointments (PIN hashing, sessions, one customer
never reaching another's bookings, repeats that skip a taken or closed
date), and the web layer end to end — including that booking needs an
account, that the diary stays behind the shop PIN, and that guessing at a
PIN gets shut out.
