# Truck Booking Bot — Navis N4 (ICTSI Durban DGT)

Books truck appointment slots on the terminal's Navis N4 web system for
AVEMEL LOG, so nobody has to sit refreshing the screen at slot-release
times. Slots appear at scheduled reviews (01:30, 06:30, 09:30, 13:30,
18:30, 21:30) **and randomly** (extra releases, other transporters
cancelling), so the bot polls continuously until every container on the
list is booked.

**Attach mode:** you log in to N4 by hand in a "debug" Chrome; the bot
connects to that same Chrome and only does the repetitive part — set
tower, enter container, read openings, Save. It never types credentials
and never touches Trucking Company or Line Operator.

## Key rules built in

- **FIFO** — the container list's order is the booking order. Report
  imports are sorted oldest In-Date first. Keep first-in at the top.
- **Booked = off the list** — a booked container is removed from
  `containers_all.csv` immediately; `results.csv` keeps the permanent
  record (and makes restarts resume cleanly).
- **One bot per tower** — concurrent N4 logins are allowed, so the
  fastest setup is a separate bot per tower, each attached to its own
  Chrome (ports: 109→9222, 202→9223, 203→9224, 205→9225).
- **Exact gate matching** — the Gate/Zone dropdown has near-duplicates
  (`109 REEFER`, `109A`…), so towers match the exact label, including
  `203 (ITZ 203 Virtual Gate)` which is worded differently.
- **Transaction type is selectable** — Pick Up Import, Drop Off Export,
  or (default, safest) "leave as hand-set in N4". Run different bots
  for different transaction types side by side.

## Setup (once)

```
pip install -r requirements.txt
copy config.example.json config.json     (edit if needed)
```

Python 3.10+ and Chrome must be installed. `playwright install` is NOT
needed — the bot attaches to your normal Chrome.

## Daily use (web UI — recommended for staff)

One-time: double-click `scripts\create_desktop_shortcuts.bat` — it puts
a **Truck Booking Bot** shortcut plus one **N4 Chrome — Tower <n>**
shortcut per tower on the Desktop, so nothing below needs the command
prompt.

1. Double-click **N4 Chrome — Tower <n>** for each tower you want to
   run (each opens its own Chrome).
2. In each Chrome: log in to N4, click **+** to open **Add
   Appointment**, hand-set **Trucking Company = AVEMEL LOG** (and the
   Transaction Type if you'll use "leave as set").
3. Double-click **Truck Booking Bot** — it installs requirements on
   first run, starts the UI, and opens http://localhost:8123 by itself.
4. Paste/import the container list, tick the towers, press **Start**.
5. Watch progress; you get a desktop toast on every booking.

## Command line

```
python -m truckbot run --tower 109                  # camp on one tower
python -m truckbot run --all                        # one session rotating towers
python -m truckbot run --tower 202 -t "Drop Off Export"
python -m truckbot run --tower 109 --debug-port 9230
python -m truckbot make-list report.txt 109         # filter an N4 report in
python -m truckbot status                           # summary + last results
python -m truckbot ui                               # the web UI
```

## Files

| file | what |
|---|---|
| `containers_all.csv` | the working list (`container,tower`), FIFO order |
| `results.csv` | permanent record: BOOKED / SKIPPED with detail |
| `n4_errors.csv` | every N4 error dialog, verbatim — see below |
| `bot.log` | run log |
| `config.json` | local settings (never commit — it's gitignored) |

### Container list rules (from operations)

Bookable = has an **In Date** AND **HOLD = null** AND status **Yard**.
Excluded: no In Date (inbound), Out Date/Departed, **EC/Out** (driver
already collecting), any hold. The report importer applies these
automatically and explains every exclusion.

### How errors are handled

- **No openings** — normal between-release state; keep polling.
- **!IMPORT RELEASE / holds** — permanent skip (fix the release, delete
  the row from `results.csv`, and it's picked up again).
- **Already has an appointment** — permanent skip.
- **Anything unrecognised** — retried (a one-off server error must not
  cost a container), but after 5 repeats of the same error the
  container is skipped so it can't wedge the run. Every dialog's FULL
  text lands in `n4_errors.csv` — when N4 shows a wording we haven't
  seen (e.g. its exact "already has an appointment" message), send that
  file so the classifier can learn it.

## Still to verify live (needs a slot-release window)

- One full booking end-to-end (Save → confirmation).
- Tower switching under real conditions.
- Exact wording of the duplicate-appointment error (auto-captured in
  `n4_errors.csv` when it happens).
- The Drop Off Export form flow (the bot's flow assumes the same
  fields as Pick Up Import; the error capture will show any difference).

## Cautions

- Automated booking may breach the terminal's terms of use; keep
  `poll_seconds` reasonable (20s+ between passes is jittered on top) —
  accounts can be suspended. It's worth asking ICTSI about official
  API/EDI access; N4 supports web-services integration.
- Container numbers transcribed from photos are OCR-risky — prefer
  exact text from PDF/CSV exports or the report importer.
- `config.json`, `results.csv` and logs are gitignored; keep them local.

## Developing

```
pip install pytest
python -m pytest tests/
```

`truckbot/` layout: `config` → settings, `session` → Chrome/CDP attach +
reconnect, `dialog` → ZK form driving (all selectors verified on the
live system — ids are random per session, so everything anchors on
stable classes + visible labels), `engine` → retry/rotation/skip logic
(browser-free, fully unit-tested), `containers`/`reportparse` → list
management, `notify` → toast/email, `webui` → the staff UI. The old
prototypes live in `legacy/` for reference.
