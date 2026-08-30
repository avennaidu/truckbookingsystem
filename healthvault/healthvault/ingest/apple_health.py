"""Apple Health `export.xml` importer (vitals only, by design).

An Apple Health export contains hundreds of thousands of samples - every
step, every heartbeat. None of that belongs in a medical history, so
this pulls only the standing measurements a clinician actually asks
about, and only the most recent of each, streaming the file rather than
loading it.
"""

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import FileImporter, ImportReport

#: HealthKit identifier -> (our name, unit)
WANTED = {
    "HKQuantityTypeIdentifierBodyMass": ("Weight", "kg"),
    "HKQuantityTypeIdentifierHeight": ("Height", "m"),
    "HKQuantityTypeIdentifierBloodPressureSystolic": ("Blood pressure (systolic)", "mmHg"),
    "HKQuantityTypeIdentifierBloodPressureDiastolic": ("Blood pressure (diastolic)", "mmHg"),
    "HKQuantityTypeIdentifierBloodGlucose": ("Blood glucose", "mmol/L"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("Oxygen saturation", "%"),
    "HKQuantityTypeIdentifierRestingHeartRate": ("Resting heart rate", "bpm"),
    "HKQuantityTypeIdentifierBodyTemperature": ("Body temperature", "degC"),
}


class AppleHealthImporter(FileImporter):
    name = "Apple Health export"
    kind = "apple_health"

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() != ".xml":
            return False
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            return False
        return "HealthKit" in head or "<HealthData" in head

    def parse(self, conn: sqlite3.Connection, path: Path, source_id: int,
              report: ImportReport) -> None:
        latest: dict[str, dict] = {}
        total = 0
        try:
            for _, element in ET.iterparse(str(path), events=("end",)):
                if element.tag != "Record":
                    continue
                total += 1
                kind = element.get("type", "")
                if kind in WANTED:
                    name, unit = WANTED[kind]
                    date = (element.get("startDate", "") or "")[:10]
                    existing = latest.get(name)
                    if existing is None or date >= existing["date"]:
                        latest[name] = {
                            "name": name,
                            "value": element.get("value", ""),
                            "unit": element.get("unit", "") or unit,
                            "date": date,
                            "panel": "Apple Health",
                        }
                element.clear()
        except ET.ParseError as exc:
            report.note(f"malformed export: {exc}")
            return
        report.note(f"scanned {total} samples, kept the latest of "
                    f"{len(latest)} standing measurements")
        for payload in latest.values():
            self.stage(conn, source_id, "observation", payload, 0.75,
                       "latest reading in your Apple Health export", report)
