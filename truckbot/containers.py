"""Container list + results store.

containers_all.csv:  container,tower   (tower optional when running --tower)
                     FILE ORDER IS BOOKING ORDER (FIFO): keep the
                     oldest-arrived containers at the top.
results.csv:         timestamp,container,status,detail  (append-only)

A container is done once results.csv has a terminal row (BOOKED/SKIPPED)
for it, so stopping and restarting the bot resumes cleanly. To re-try a
skipped container (e.g. after a customs release clears), delete its row.
"""

import csv
import threading
from datetime import datetime
from pathlib import Path

TERMINAL = {"BOOKED", "SKIPPED"}


class ResultsStore:
    """Append-only results.csv with an in-memory done-set."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._done: dict[str, str] = {}     # container -> terminal status
        self._rows: list[dict] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        with open(self.path, newline="") as f:
            for r in csv.DictReader(f):
                self._rows.append(r)
                if r.get("status") in TERMINAL:
                    self._done[r["container"]] = r["status"]

    def record(self, container: str, status: str, detail: str = ""):
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "container": container,
            "status": status,
            "detail": detail,
        }
        with self._lock:
            new = not self.path.exists()
            with open(self.path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "container", "status", "detail"])
                w.writerow([row["timestamp"], container, status, detail])
            self._rows.append(row)
            if status in TERMINAL:
                self._done[container] = status

    def done_set(self) -> set[str]:
        with self._lock:
            return set(self._done)

    def rows(self) -> list[dict]:
        with self._lock:
            return list(self._rows)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.rows():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return counts


def load_containers(path: Path | str, done: set[str],
                    only_tower: str | None = None) -> list[dict]:
    """Pending {container, tower} rows in file order.

    Accepts both formats: container,tower (all-towers) and a plain single
    'container' column (single-tower; tower filled from only_tower).
    """
    path = Path(path)
    if not path.exists():
        return []
    out, seen = [], set()
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            c = (r.get("container") or "").strip().upper()
            t = (r.get("tower") or "").strip() or (only_tower or "")
            if not c or c in done or c in seen:
                continue
            if only_tower and t and t != only_tower:
                continue
            seen.add(c)
            out.append({"container": c, "tower": t})
    return out


def group_by_tower(pending: list[dict], tower_order: list[str]) -> dict[str, list[str]]:
    """{tower: [containers...]} preserving file order, towers in tower_order
    first, then any others."""
    buckets: dict[str, list[str]] = {}
    for item in pending:
        buckets.setdefault(item["tower"], []).append(item["container"])
    ordered: dict[str, list[str]] = {}
    for t in tower_order:
        if t in buckets:
            ordered[t] = buckets[t]
    for t, v in buckets.items():
        ordered.setdefault(t, v)
    return ordered


class ErrorCapture:
    """Verbatim N4 error log (n4_errors.csv).

    N4's exact wording for several errors (e.g. "already has an
    appointment") is still unknown - every dialog the bot dismisses is
    recorded IN FULL here so the wording can be added to the classifier.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def capture(self, container: str, stage: str, text: str, classified: str):
        with self._lock:
            new = not self.path.exists()
            with open(self.path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "container", "stage",
                                "classified_as", "full_text"])
                w.writerow([datetime.now().isoformat(timespec="seconds"),
                            container, stage, classified,
                            (text or "").strip()])


def remove_container(path: Path | str, container: str) -> bool:
    """Physically drop a booked container from the list file.

    Bookings are the record in results.csv; operations also wants the
    row GONE from the working list. Rewrites atomically (tmp + replace).
    With one bot per tower the towers' rows are disjoint, so concurrent
    removals can't fight over the same row; results.csv still filters
    as a belt-and-braces even if a rewrite ever races.
    """
    path = Path(path)
    if not path.exists():
        return False
    container = container.strip().upper()
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
        fields = ["container", "tower"]
    kept = [r for r in rows
            if (r.get("container") or "").strip().upper() != container]
    if len(kept) == len(rows):
        return False
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in kept:
            w.writerow([r.get("container", ""), r.get("tower", "")])
    tmp.replace(path)
    return True
