"""Importers. Each one stages candidates; none writes to the record."""

from pathlib import Path

from .apple_health import AppleHealthImporter
from .base import ImportReport, choose
from .fhir import FHIRImporter
from .generic import CSVImporter, DocumentImporter
from .medical_aid import MedicalAidImporter

#: Order matters - most specific first, catch-alls last.
IMPORTERS = [
    FHIRImporter(),
    AppleHealthImporter(),
    MedicalAidImporter(),
    CSVImporter(),
    DocumentImporter(),
]


def import_file(conn, path: Path | str, table: str = "") -> ImportReport:
    """Pick the right importer for `path` and run it."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    importer = CSVImporter(table) if table else choose(path, IMPORTERS)
    if importer is None:
        report = ImportReport(source=path.name)
        report.note("no importer recognised this file. For a plain CSV, say "
                    "which kind of list it is (--as medication, --as allergy...).")
        return report
    return importer.load(conn, path)


__all__ = ["IMPORTERS", "import_file", "ImportReport", "AppleHealthImporter",
           "FHIRImporter", "CSVImporter", "DocumentImporter", "MedicalAidImporter"]
