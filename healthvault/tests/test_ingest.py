"""Importers: what they understand, and what they refuse to assert."""

import json

import pytest

from healthvault import store
from healthvault.extract.classify import classify, find_date, find_observations
from healthvault.ingest import import_file
from healthvault.ingest.medical_aid import map_columns

CLAIMS = """Service Date,Provider Name,Practice Number,Discipline,Tariff Description,Claimed Amount,Scheme Paid,Claim Number
14/03/2026,DR S MOODLEY,0123456,General Practitioner,Consultation,650.00,520.00,C1
02/04/2026,CLICKS PHARMACY,0777111,Pharmacy,METFORMIN 850MG TAB 60,180.00,180.00,C2
05/04/2026,LIFE HOSPITAL,0555222,Hospital,Admission - day case,15400.00,15400.00,C3
01/05/2026,,,,Savings contribution,500.00,0.00,C4
"""

BUNDLE = {"resourceType": "Bundle", "entry": [
    {"resource": {"resourceType": "AllergyIntolerance",
                  "code": {"text": "Penicillin"}, "criticality": "high",
                  "reaction": [{"manifestation": [{"text": "Anaphylaxis"}],
                                "severity": "severe"}]}},
    {"resource": {"resourceType": "Condition",
                  "code": {"text": "Type 2 diabetes",
                           "coding": [{"code": "E11", "system": "icd-10"}]},
                  "onsetDateTime": "2019-04-01T00:00:00Z"}},
    {"resource": {"resourceType": "Patient", "id": "ignored"}}]}


def staged(conn, table):
    return [json.loads(r["payload"]) for r in store.pending(conn)
            if r["table_name"] == table]


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


# -- medical aid -------------------------------------------------------

def test_claims_become_visits(conn, tmp_path):
    report = import_file(conn, write(tmp_path, "claims.csv", CLAIMS))
    assert report.staged == 3                      # the contribution line is noise
    visits = staged(conn, "encounter")
    assert {v["kind"] for v in visits} == {"consult", "admission"}
    assert visits[0]["date"] == "2026-03-14"       # dd/mm/yyyy read correctly


def test_pharmacy_lines_become_medication_candidates(conn, tmp_path):
    import_file(conn, write(tmp_path, "claims.csv", CLAIMS))
    meds = staged(conn, "medication")
    assert [m["name"] for m in meds] == ["METFORMIN 850MG"]
    assert meds[0]["status"] == "unknown"          # not asserted as current


def test_claims_never_assert_a_diagnosis(conn, tmp_path):
    """A billing code is evidence of a visit, not proof of a condition."""
    import_file(conn, write(tmp_path, "claims.csv", CLAIMS))
    assert staged(conn, "condition") == []


def test_claim_confidence_stays_low(conn, tmp_path):
    import_file(conn, write(tmp_path, "claims.csv", CLAIMS))
    assert all(r["confidence"] <= 0.6 for r in store.pending(conn))


def test_reimporting_a_statement_is_idempotent(conn, tmp_path):
    path = write(tmp_path, "claims.csv", CLAIMS)
    import_file(conn, path)
    again = import_file(conn, path)
    assert again.staged == 0 and again.duplicates == 3


def test_column_aliases_cope_with_different_schemes():
    discovery = map_columns(["Service Date", "Provider Name", "Claimed Amount"])
    other = map_columns(["Date Of Service", "Practitioner", "Amount Claimed"])
    assert discovery["date"] == "Service Date"
    assert other["date"] == "Date Of Service"
    assert other["provider"] == "Practitioner"


# -- FHIR --------------------------------------------------------------

def test_fhir_bundle(conn, tmp_path):
    report = import_file(conn, write(tmp_path, "b.json", json.dumps(BUNDLE)))
    assert report.staged == 2 and report.skipped == 1
    assert staged(conn, "allergy")[0]["reaction"] == "Anaphylaxis"
    condition = staged(conn, "condition")[0]
    assert condition["code"] == "E11" and condition["onset"] == "2019-04-01"


def test_fhir_is_trusted_more_than_a_claim_line(conn, tmp_path):
    import_file(conn, write(tmp_path, "b.json", json.dumps(BUNDLE)))
    assert all(r["confidence"] >= 0.9 for r in store.pending(conn))


# -- plain lists -------------------------------------------------------

def test_a_hand_written_list_imports_row_by_row(conn, tmp_path):
    path = write(tmp_path, "meds.csv",
                 "Medication,Dose,How often\nMetformin,1 g,twice daily\n"
                 "Enalapril,10 mg,daily\n")
    import_file(conn, path)
    meds = staged(conn, "medication")
    assert {m["name"] for m in meds} == {"Metformin", "Enalapril"}
    assert meds[0]["frequency"] == "twice daily"     # header alias applied


def test_an_unrecognised_file_is_reported_not_guessed(conn, tmp_path):
    report = import_file(conn, write(tmp_path, "x.csv", "a,b\n1,2\n"))
    assert report.staged == 0
    assert any("could not tell" in note or "no importer" in note
               for note in report.notes)


def test_missing_file_raises(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_file(conn, tmp_path / "nope.csv")


# -- classification ----------------------------------------------------

def test_lab_reports_are_recognised_and_read():
    text = ("AMPATH PATHOLOGY Specimen collected 14/03/2026 Reference Range "
            "HbA1c 7.4 % Creatinine 88 umol/L")
    kind, confidence = classify(text, "results.pdf")
    assert kind == "lab_result" and confidence > 0.5
    values = {o["name"]: o["value"] for o in find_observations(text)}
    assert values["HbA1c"] == "7.4" and values["Creatinine"] == "88"


def test_ordinary_mail_is_not_mistaken_for_a_record():
    assert classify("Lunch on Tuesday?", "note.txt") == ("unknown", 0.0)


@pytest.mark.parametrize("raw,expected", [
    ("2026-01-09", "2026-01-09"),
    ("14/03/2026", "2026-03-14"),        # South African day/month order
    ("9 Jan 2026", "2026-01-09"),
])
def test_dates_are_normalised(raw, expected):
    assert find_date(f"seen on {raw} by") == expected
