"""Parse a pasted 'Units assigned to trucking company' N4 report into
bookable containers (the make_list logic).

Eligibility (per operations):
  BOOKABLE = has an In Date AND HOLD ON UNIT is null AND status is Yard
  EXCLUDED = no In Date (still inbound on vessel)
           | Out Date / status Departed (already picked up)
           | status EC/Out (a driver is already collecting it)
           | HOLD anything other than null

Vessels change constantly but the four towers are permanent, so no
vessel->tower mapping: the caller supplies the tower.

FIFO: bookings must go first-in-first-out, so bookable containers are
sorted by In Date (oldest first) before being appended - and the list
file's order is the booking order.
"""

from datetime import datetime

import csv
import re
from pathlib import Path

CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\b")


def _parse_in_date(s: str) -> datetime:
    """'12/08/26 09:15' or '12/08/2026 09:15' -> datetime (for FIFO sort)."""
    for fmt in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.max      # unparseable dates sort last


def parse_report(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Returns (bookable, [(excluded_container, why), ...]).
    Bookable is FIFO-ordered: oldest In Date first."""
    bookable, excluded = [], []
    for raw in text.splitlines():
        line = raw.strip()
        m = CONTAINER_RE.search(line)
        if not m:
            continue
        container = m.group(1)
        rest = line[m.end():]
        low = rest.lower()

        dates = DATE_RE.findall(rest)
        has_in = len(dates) >= 1
        departed = "departed" in low or len(dates) >= 2   # 2 dates = In+Out
        ecout = "ec/out" in low or "ec /out" in low or "ec/ out" in low
        hold = rest.split()[-1].lower() if rest.split() else ""
        hold_clear = hold in ("null", "none", "-", "")

        if has_in and hold_clear and not departed and not ecout:
            bookable.append((_parse_in_date(dates[0]), container))
        else:
            why = []
            if not has_in:
                why.append("inbound")
            if departed:
                why.append("departed")
            if ecout:
                why.append("EC/Out")
            if not hold_clear:
                why.append("hold=" + hold)
            excluded.append((container, ",".join(why) or "excluded"))
    # FIFO: oldest In Date first (stable, so report order breaks ties),
    # then de-dup keeping the first occurrence
    bookable.sort(key=lambda p: p[0])
    seen: set[str] = set()
    ordered = [c for _, c in bookable
               if not (c in seen or seen.add(c))]
    return ordered, excluded


def append_to_list(containers: list[str], tower: str,
                   out: Path | str) -> list[str]:
    """Append new containers to containers_all.csv (skip duplicates).
    Returns the containers actually added."""
    out = Path(out)
    rows = list(csv.DictReader(open(out))) if out.exists() else []
    have = {r["container"] for r in rows}
    new = [c for c in containers if c not in have]
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["container", "tower"])
        for r in rows:
            w.writerow([r["container"], r["tower"]])
        for c in new:
            w.writerow([c, tower])
    return new
