"""Medical-aid claims importer.

Schemes do not offer a patient API, but every one of them lets you
download a claims statement, and that statement is a surprisingly
complete index of your medical life: who you saw, when, where, and what
was done. This importer turns those lines into visits.

Column names differ per scheme (Discovery, Bonitas, Momentum, Medihelp,
Bestmed all differ), so columns are matched by ALIAS rather than
position, and anything unmatched is preserved in notes.

What it deliberately does NOT do is infer diagnoses. A claim for a
diabetic-retinopathy screening is evidence, not a diagnosis, and writing
"diabetes" into a medical history off a billing code is exactly the kind
of confident wrongness that makes a record untrustworthy. Claims become
encounters, and where the discipline is a pharmacy, dispensed-medicine
candidates - both at modest confidence, both reviewed by you.
"""

import csv
import re
import sqlite3
from pathlib import Path

from ..extract.classify import _normalise_date
from .base import FileImporter, ImportReport

#: our field -> header aliases, lower-cased and stripped of punctuation.
ALIASES = {
    "date": ["service date", "date of service", "treatment date", "servicedate",
             "date", "from date", "claim date", "date from"],
    "provider": ["provider name", "provider", "service provider", "practitioner",
                 "supplier name", "doctor", "provider description"],
    "practice_number": ["practice number", "practice no", "provider number",
                        "bhf number", "pr number", "practice"],
    "discipline": ["discipline", "provider type", "speciality", "specialty",
                   "provider discipline", "service type"],
    "description": ["description", "tariff description", "service description",
                    "treatment", "item description", "detail", "narrative"],
    "code": ["tariff code", "tariff", "code", "item code", "nappi", "nappi code",
             "procedure code"],
    "claimed": ["claimed amount", "claimed", "amount claimed", "total claimed",
                "gross amount", "amount"],
    "paid": ["scheme paid", "paid", "amount paid", "benefit paid", "scheme portion"],
    "member_portion": ["member portion", "member liable", "co-payment", "copayment",
                       "you owe", "patient portion", "short paid"],
    "patient": ["patient", "dependant", "dependent", "member", "beneficiary",
                "patient name", "dependant code"],
    "claim_number": ["claim number", "claim no", "claim ref", "reference"],
}

#: Disciplines whose lines are dispensed medicine rather than a visit.
PHARMACY_HINTS = ("pharmac", "dispens", "chemist", "medicine", "medication", "drug")

#: Lines that are money, not care - never become part of a history.
NOISE = ("levy", "co-payment adjustment", "interest", "balance brought forward",
         "opening balance", "savings contribution", "premium", "membership fee")

CONFIDENCE_VISIT = 0.6
CONFIDENCE_MED = 0.45


def normalise_header(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()


def map_columns(headers: list[str]) -> dict[str, str]:
    """Match a statement's headers onto our fields. Longest alias wins."""
    found: dict[str, str] = {}
    clean = {normalise_header(h): h for h in headers}
    for field, aliases in ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            for norm, original in clean.items():
                if norm == alias or norm.replace(" ", "") == alias.replace(" ", ""):
                    found[field] = original
                    break
            if field in found:
                break
    # Fall back to a looser containment match for anything still missing.
    for field, aliases in ALIASES.items():
        if field in found:
            continue
        for norm, original in clean.items():
            if any(alias in norm for alias in aliases) and original not in found.values():
                found[field] = original
                break
    return found


def looks_like_pharmacy(row: dict) -> bool:
    blob = " ".join(str(row.get(k, "")) for k in ("discipline", "provider", "description")).lower()
    return any(hint in blob for hint in PHARMACY_HINTS)


def is_noise(row: dict) -> bool:
    blob = " ".join(str(row.get(k, "")) for k in ("description", "code")).lower()
    return any(word in blob for word in NOISE)


class MedicalAidImporter(FileImporter):
    name = "Medical aid claims"
    kind = "medical_aid"

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() not in (".csv", ".tsv", ".txt"):
            return False
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                header = fh.readline()
        except OSError:
            return False
        mapped = map_columns(next(csv.reader([header]), []))
        # A claims file is characterised by money plus a provider or code.
        return bool(
            "date" in mapped
            and {"claimed", "paid", "member_portion"} & set(mapped)
            and {"provider", "code", "description"} & set(mapped))

    def parse(self, conn: sqlite3.Connection, path: Path, source_id: int,
              report: ImportReport) -> None:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            sample = fh.read(8192)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(fh, dialect=dialect)
            headers = reader.fieldnames or []
            mapping = map_columns(headers)
            if "date" not in mapping:
                report.note("no service-date column found - is this a claims export?")
                return
            report.note(f"matched columns: {', '.join(sorted(mapping))}")
            unmapped = [h for h in headers if h not in mapping.values()]
            for raw in reader:
                row = {field: (raw.get(col) or "").strip()
                       for field, col in mapping.items()}
                if not any(row.values()):
                    continue
                if is_noise(row):
                    report.skipped += 1
                    continue
                extra = {h: (raw.get(h) or "").strip() for h in unmapped
                         if (raw.get(h) or "").strip()}
                self._stage_row(conn, source_id, row, extra, report)

    def _stage_row(self, conn, source_id, row, extra, report):
        date = _normalise_date(row.get("date", ""))
        provider = row.get("provider", "")
        description = row.get("description", "")
        key_bits = [date, provider, description or row.get("code", ""),
                    row.get("claim_number", "")]
        dedup = "claim|" + "|".join(b.lower() for b in key_bits if b)

        if looks_like_pharmacy(row) and description:
            payload = {
                "name": _clean_medicine(description),
                "prescriber": provider,
                "started": date,
                "status": "unknown",
                "notes": _notes(row, extra),
            }
            self.stage(conn, source_id, "medication", payload, CONFIDENCE_MED,
                       "dispensed on a pharmacy claim - confirm you still take it",
                       report, dedup_key=dedup + "|med")
            return

        payload = {
            "date": date,
            "kind": _kind_for(row),
            "provider": provider,
            "facility": row.get("practice_number", ""),
            "reason": description,
            "cost": row.get("claimed", "") or row.get("paid", ""),
            "summary": "",
            "notes": _notes(row, extra),
        }
        self.stage(conn, source_id, "encounter", payload, CONFIDENCE_VISIT,
                   "claim line from a medical-aid statement", report,
                   dedup_key=dedup + "|visit")


def _kind_for(row: dict) -> str:
    blob = " ".join(str(row.get(k, "")) for k in ("discipline", "description")).lower()
    for hint, kind in (("hospital", "admission"), ("casualty", "casualty"),
                       ("emergency", "casualty"), ("radiolog", "imaging"),
                       ("patholog", "pathology"), ("dentist", "dental"),
                       ("optom", "optometry"), ("physio", "physiotherapy"),
                       ("anaesth", "procedure"), ("surg", "procedure")):
        if hint in blob:
            return kind
    return "consult"


def _clean_medicine(description: str) -> str:
    """Strip pack sizes and billing noise off a dispensed-item line."""
    text = re.sub(r"\b(tab|cap|susp|inj)\b.*$", "", description, flags=re.I)
    text = re.sub(r"\s*\d+\s*(?:x|/)\s*\d+\s*$", "", text)
    return text.strip(" -,") or description.strip()


def _notes(row: dict, extra: dict) -> str:
    parts = []
    for field in ("discipline", "code", "member_portion", "patient", "claim_number"):
        if row.get(field):
            parts.append(f"{field}={row[field]}")
    parts += [f"{k}={v}" for k, v in sorted(extra.items())]
    return "; ".join(parts)
