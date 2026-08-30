"""The importer contract.

Every importer does exactly one thing: turn a file (or a mailbox) into
candidate records in the review queue. None of them may touch a real
clinical table - `store.stage` is the only door, and it refuses any table
outside `RECORD_TABLES`.

An importer reports what it did as an `ImportReport` so the UI can show
"41 candidates, 12 already known, 3 unreadable" instead of a bare count.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .. import db, store


@dataclass
class ImportReport:
    source: str
    staged: int = 0
    duplicates: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def add(self, staged_id: int | None) -> None:
        if staged_id is None:
            self.duplicates += 1
        else:
            self.staged += 1

    def summary(self) -> str:
        bits = [f"{self.staged} for review"]
        if self.duplicates:
            bits.append(f"{self.duplicates} already known")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return f"{self.source}: " + ", ".join(bits)


class Importer(Protocol):
    name: str
    kind: str

    def sniff(self, path: Path) -> bool:
        """Cheap check: does this file look like mine?"""

    def load(self, conn: sqlite3.Connection, path: Path) -> ImportReport:
        """Parse and stage. Must not write to clinical tables."""


class FileImporter:
    """Shared plumbing: open a source row, then stage against it."""

    name = "file"
    kind = "csv"

    def sniff(self, path: Path) -> bool:            # pragma: no cover
        raise NotImplementedError

    def load(self, conn: sqlite3.Connection, path: Path) -> ImportReport:
        source_id = db.add_source(conn, self.kind, f"{self.name}: {path.name}",
                                  path=str(path))
        report = ImportReport(source=self.name)
        self.parse(conn, path, source_id, report)
        return report

    def parse(self, conn, path, source_id, report):  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def stage(conn, source_id, table, payload, confidence, reason, report,
              dedup_key: str = ""):
        report.add(store.stage(conn, source_id, table, payload,
                               confidence=confidence, reason=reason,
                               dedup_key=dedup_key))


def choose(path: Path, importers) -> "Importer | None":
    """First importer that recognises the file."""
    for importer in importers:
        try:
            if importer.sniff(path):
                return importer
        except Exception:
            continue
    return None
