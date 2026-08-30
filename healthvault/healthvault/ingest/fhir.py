"""FHIR R4 Bundle importer - the standards-compliant path.

When a hospital, scheme or app offers a real export, this is usually the
format (Apple Health Records, most patient portals, anything built on
HL7 FHIR). It is the highest-confidence source we handle, because the
fields are typed rather than inferred, so entries land at 0.9.
"""

import json
import sqlite3
from pathlib import Path

from .base import FileImporter, ImportReport

#: FHIR resourceType -> our table.
RESOURCE_MAP = {
    "AllergyIntolerance": "allergy",
    "Condition": "condition",
    "MedicationStatement": "medication",
    "MedicationRequest": "medication",
    "Immunization": "immunisation",
    "Observation": "observation",
    "Procedure": "procedure",
    "Encounter": "encounter",
}

CONFIDENCE = 0.9


def _text(node) -> str:
    """A CodeableConcept's human label, however it was expressed."""
    if not isinstance(node, dict):
        return str(node or "")
    if node.get("text"):
        return str(node["text"])
    for coding in node.get("coding", []) or []:
        if coding.get("display"):
            return str(coding["display"])
        if coding.get("code"):
            return str(coding["code"])
    return ""


def _code(node) -> tuple[str, str]:
    for coding in (node or {}).get("coding", []) or []:
        if coding.get("code"):
            return str(coding["code"]), str(coding.get("system", ""))
    return "", ""


def _date(resource, *keys) -> str:
    for key in keys:
        value = resource.get(key)
        if isinstance(value, str) and value:
            return value[:10]
        if isinstance(value, dict):
            for inner in ("start", "dateTime", "date"):
                if value.get(inner):
                    return str(value[inner])[:10]
    return ""


class FHIRImporter(FileImporter):
    name = "FHIR bundle"
    kind = "fhir"

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() != ".json":
            return False
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            return False
        return '"resourceType"' in head and (
            '"Bundle"' in head or '"entry"' in head)

    def parse(self, conn: sqlite3.Connection, path: Path, source_id: int,
              report: ImportReport) -> None:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            report.note(f"could not read bundle: {exc}")
            return
        entries = bundle.get("entry") or []
        if not entries and bundle.get("resourceType") in RESOURCE_MAP:
            entries = [{"resource": bundle}]
        for entry in entries:
            resource = entry.get("resource") or {}
            table = RESOURCE_MAP.get(resource.get("resourceType", ""))
            if not table:
                report.skipped += 1
                continue
            payload = self._convert(table, resource)
            if not payload:
                report.skipped += 1
                continue
            self.stage(conn, source_id, table, payload, CONFIDENCE,
                       f"FHIR {resource.get('resourceType')}", report)

    def _convert(self, table: str, res: dict) -> dict | None:
        if table == "allergy":
            substance = _text(res.get("code"))
            if not substance:
                return None
            reactions = res.get("reaction") or []
            manifestation = ""
            severity = res.get("criticality", "")
            if reactions:
                first = reactions[0]
                manifestation = ", ".join(
                    filter(None, (_text(m) for m in first.get("manifestation", []))))
                severity = first.get("severity", "") or severity
            return {"substance": substance, "reaction": manifestation,
                    "severity": _severity(severity),
                    "onset": _date(res, "onsetDateTime", "recordedDate"),
                    "status": _clinical_status(res)}
        if table == "condition":
            name = _text(res.get("code"))
            if not name:
                return None
            code, system = _code(res.get("code"))
            return {"name": name, "code": code, "code_system": system,
                    "status": _clinical_status(res),
                    "onset": _date(res, "onsetDateTime", "onsetPeriod", "recordedDate"),
                    "severity": _text(res.get("severity"))}
        if table == "medication":
            name = (_text(res.get("medicationCodeableConcept"))
                    or _text(res.get("medicationReference", {}).get("display", "")))
            if not name:
                return None
            dosage = (res.get("dosage") or res.get("dosageInstruction") or [{}])[0]
            return {"name": name, "frequency": dosage.get("text", ""),
                    "route": _text(dosage.get("route")),
                    "status": res.get("status", "active"),
                    "started": _date(res, "effectiveDateTime", "effectivePeriod",
                                     "authoredOn"),
                    "reason": _text((res.get("reasonCode") or [{}])[0])}
        if table == "immunisation":
            vaccine = _text(res.get("vaccineCode"))
            if not vaccine:
                return None
            return {"vaccine": vaccine, "date": _date(res, "occurrenceDateTime"),
                    "batch": res.get("lotNumber", ""),
                    "provider": _text(res.get("location", {}).get("display", ""))}
        if table == "observation":
            name = _text(res.get("code"))
            if not name:
                return None
            quantity = res.get("valueQuantity") or {}
            value = (str(quantity.get("value", "")) or _text(res.get("valueCodeableConcept"))
                     or str(res.get("valueString", "")))
            ranges = res.get("referenceRange") or [{}]
            low = (ranges[0].get("low") or {}).get("value", "")
            high = (ranges[0].get("high") or {}).get("value", "")
            return {"name": name, "value": value, "unit": quantity.get("unit", ""),
                    "ref_range": f"{low}-{high}" if low != "" or high != "" else "",
                    "abnormal": int(bool(res.get("interpretation"))),
                    "date": _date(res, "effectiveDateTime", "issued")}
        if table == "procedure":
            name = _text(res.get("code"))
            if not name:
                return None
            return {"name": name, "date": _date(res, "performedDateTime", "performedPeriod"),
                    "facility": _text(res.get("location", {}).get("display", ""))}
        if table == "encounter":
            return {"date": _date(res, "period", "plannedStartDate"),
                    "kind": _text(res.get("class")) or res.get("status", ""),
                    "reason": _text((res.get("reasonCode") or [{}])[0]),
                    "facility": _text((res.get("location") or [{}])[0]
                                      .get("location", {}).get("display", ""))}
        return None


def _clinical_status(res: dict) -> str:
    status = _text(res.get("clinicalStatus")) or res.get("status", "")
    return {"resolved": "resolved", "inactive": "resolved",
            "active": "active", "confirmed": "active"}.get(status.lower(), status or "active")


def _severity(value: str) -> str:
    return {"high": "severe", "low": "mild", "unable-to-assess": ""}.get(
        (value or "").lower(), (value or "").lower())
