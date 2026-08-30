"""Two catch-all importers for the files that fit no standard.

`CSVImporter` takes any CSV whose headers name our fields, so an export
from a clinic spreadsheet or a list you typed yourself imports without
code. `DocumentImporter` takes a single PDF or scan and catalogues it.
"""

import csv
import sqlite3
from pathlib import Path

from .. import RECORD_TABLES
from ..extract.classify import classify, find_date, find_observations
from ..extract.text import extract_text, sha256_of
from .base import FileImporter, ImportReport
from .medical_aid import normalise_header

#: Friendly header -> the column it means, per table. A hand-made list
#: says "Medication, Dose"; the schema says "name, dose".
COLUMN_ALIASES = {
    "allergy": {"allergy": "substance", "allergen": "substance", "name": "substance",
                "symptoms": "reaction", "reaction_to": "reaction"},
    "medication": {"medication": "name", "medicine": "name", "drug": "name",
                   "strength": "dose", "how_often": "frequency", "taken_for": "reason",
                   "doctor": "prescriber"},
    "condition": {"condition": "name", "diagnosis": "name", "problem": "name",
                  "diagnosed": "onset"},
    "immunisation": {"vaccination": "vaccine", "immunisation": "vaccine",
                     "name": "vaccine", "given": "date", "lot": "batch"},
    "observation": {"test": "name", "measurement": "name", "result": "value",
                    "reading": "value", "range": "ref_range", "units": "unit"},
    "procedure": {"procedure": "name", "operation": "name", "surgery": "name",
                  "performed": "date", "surgeon": "provider", "hospital": "facility"},
}


def apply_aliases(table: str, payload: dict) -> dict:
    """Rename friendly headers to schema columns, keeping the rest."""
    aliases = COLUMN_ALIASES.get(table, {})
    out: dict = {}
    for key, value in payload.items():
        out[aliases.get(key, key)] = value
    return out


#: Header sets that identify what a plain CSV is a list OF.
TABLE_SIGNATURES = {
    "allergy": {"substance", "allergy", "allergen"},
    "medication": {"medication", "medicine", "drug", "name"},
    "condition": {"condition", "diagnosis", "problem"},
    "immunisation": {"vaccine", "vaccination", "immunisation"},
    "observation": {"observation", "test", "result", "measurement"},
    "procedure": {"procedure", "operation", "surgery"},
}


def guess_table(headers: list[str]) -> str:
    clean = {normalise_header(h) for h in headers}
    best, score = "", 0
    for table, words in TABLE_SIGNATURES.items():
        hits = len(clean & words)
        if hits > score:
            best, score = table, hits
    return best


class CSVImporter(FileImporter):
    name = "CSV list"
    kind = "csv"

    def __init__(self, table: str = ""):
        self.table = table

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() not in (".csv", ".tsv"):
            return False
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                headers = next(csv.reader([fh.readline()]), [])
        except OSError:
            return False
        return bool(self.table or guess_table(headers))

    def parse(self, conn: sqlite3.Connection, path: Path, source_id: int,
              report: ImportReport) -> None:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            table = self.table or guess_table(headers)
            if table not in RECORD_TABLES:
                report.note(f"could not tell what this is a list of: {headers}")
                return
            report.note(f"reading as {table} records")
            for raw in reader:
                payload = apply_aliases(table, {
                    normalise_header(k).replace(" ", "_"): (v or "").strip()
                    for k, v in raw.items() if k})
                if not any(payload.values()):
                    continue
                self.stage(conn, source_id, table, payload, 0.7,
                           f"row from {path.name}", report)


class DocumentImporter(FileImporter):
    name = "Document"
    kind = "pdf"

    def sniff(self, path: Path) -> bool:
        return path.suffix.lower() in (".pdf", ".txt", ".md")

    def parse(self, conn: sqlite3.Connection, path: Path, source_id: int,
              report: ImportReport) -> None:
        text = extract_text(path)
        kind, confidence = classify(text, path.name)
        if not text:
            report.note("no text could be read - filed as an image/scan only")
        self.stage(conn, source_id, "document", {
            "title": path.stem.replace("_", " "),
            "kind": kind,
            "date": find_date(text),
            "path": str(path),
            "sha256": sha256_of(path),
            "text": text[:20000],
        }, max(confidence, 0.5), f"imported file {path.name}", report,
            dedup_key="doc|" + sha256_of(path))
        if kind == "lab_result":
            for observation in find_observations(text):
                observation["panel"] = path.stem
                self.stage(conn, source_id, "observation", observation, 0.55,
                           f"read off {path.name}", report)
