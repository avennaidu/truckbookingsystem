"""
make_list.py - add bookable containers from an N4 report to containers_all.csv

You supply the TOWER on the command line (towers are fixed: 109/202/203/205).
No vessel-name mapping is needed - vessels change, towers don't.

Eligibility rules (per operations):
  BOOKABLE = has an In Date  AND  HOLD ON UNIT is null  AND  status is Yard
  EXCLUDED = no In Date (still inbound on vessel)
           | Out Date / status Departed (already picked up)
           | status EC/Out (a driver is already collecting it)
           | HOLD is anything other than null

Get the report text: open the "Units assigned to trucking company" report,
Ctrl+A, Ctrl+C, paste into report.txt.

Usage:
    python make_list.py report.txt 109       -> appends 109 containers
    python make_list.py report.txt 203       -> appends 203 containers

Containers already in containers_all.csv are skipped (no duplicates).
"""

import csv
import re
import sys
from pathlib import Path

OUT = Path("containers_all.csv")
VALID_TOWERS = {"109", "202", "203", "205"}

CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\b")


def parse(text):
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
        departed = "departed" in low or len(dates) >= 2
        ecout = "ec/out" in low or "ec /out" in low or "ec/ out" in low
        hold = rest.split()[-1].lower() if rest.split() else ""
        hold_clear = hold in ("null", "none", "-", "")

        if has_in and hold_clear and not departed and not ecout:
            bookable.append(container)
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
    return bookable, excluded


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    tower = sys.argv[2].strip()
    if tower not in VALID_TOWERS:
        print("Tower must be one of", sorted(VALID_TOWERS), "- got", tower)
        sys.exit(1)

    text = src.read_text(encoding="utf-8", errors="ignore")
    bookable, excluded = parse(text)

    seen = set()
    bookable = [c for c in bookable if not (c in seen or seen.add(c))]

    rows = list(csv.DictReader(open(OUT))) if OUT.exists() else []
    have = {r["container"] for r in rows}
    new = [c for c in bookable if c not in have]

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["container", "tower"])
        for r in rows:
            w.writerow([r["container"], r["tower"]])
        for c in new:
            w.writerow([c, tower])

    print("Tower " + tower + ": added " + str(len(new)) + " new (of "
          + str(len(bookable)) + " bookable) -> " + str(OUT))
    for c in new:
        print("  ", c)
    if excluded:
        print("Excluded " + str(len(excluded)) + ":")
        for c, why in excluded:
            print("  ", c, "(" + why + ")")


if __name__ == "__main__":
    main()
