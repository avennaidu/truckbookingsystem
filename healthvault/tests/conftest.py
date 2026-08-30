import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from healthvault import db, store            # noqa: E402


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def source(conn):
    return db.add_source(conn, "csv", "test source")


@pytest.fixture
def populated(conn):
    """A small but realistic record: two allergies, conditions, meds, ICE."""
    store.save_profile(conn, dict(
        full_name="Aven Naidu", dob="1980-05-02", sex="M", blood_type="O+",
        organ_donor=1, scheme="Discovery Health", plan="Classic Saver",
        member_number="1234567890"))
    store.insert(conn, "allergy", dict(
        substance="Penicillin", reaction="anaphylaxis",
        severity="life-threatening"))
    store.insert(conn, "allergy", dict(
        substance="Latex", reaction="hives", severity="moderate"))
    store.insert(conn, "condition", dict(name="Type 2 diabetes", on_card=1))
    store.insert(conn, "condition", dict(name="Old fracture", status="resolved",
                                         on_card=1))
    store.insert(conn, "medication", dict(name="Metformin", dose="1 g",
                                          frequency="BD", on_card=1))
    store.insert(conn, "emergency_contact", dict(
        name="Priya Naidu", relationship="spouse", phone="+27 82 555 0134"))
    return conn
