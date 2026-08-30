"""The review queue is the safety property: importers must not be able
to write to the record, and repeat imports must not create twins."""

import json

import pytest

from healthvault import store


def test_staged_records_are_not_in_the_record_yet(conn, source):
    store.stage(conn, source, "allergy", {"substance": "Penicillin"})
    assert store.rows(conn, "allergy") == []
    assert len(store.pending(conn)) == 1


def test_approving_moves_it_into_the_record(conn, source):
    staged_id = store.stage(conn, source, "allergy", {"substance": "Penicillin"})
    store.approve(conn, staged_id)
    rows = store.rows(conn, "allergy")
    assert [r["substance"] for r in rows] == ["Penicillin"]
    assert store.pending(conn) == []


def test_approved_rows_keep_their_source(conn, source):
    staged_id = store.stage(conn, source, "allergy", {"substance": "Penicillin"})
    store.approve(conn, staged_id)
    assert store.rows(conn, "allergy")[0]["source_id"] == source


def test_rejecting_leaves_the_record_untouched(conn, source):
    staged_id = store.stage(conn, source, "allergy", {"substance": "Penicillin"})
    store.reject(conn, staged_id)
    assert store.rows(conn, "allergy") == []
    assert store.pending(conn) == []


def test_a_record_cannot_be_approved_twice(conn, source):
    staged_id = store.stage(conn, source, "condition", {"name": "Asthma"})
    store.approve(conn, staged_id)
    with pytest.raises(KeyError):
        store.approve(conn, staged_id)


def test_importers_cannot_reach_arbitrary_tables(conn, source):
    for table in ("share", "profile", "sqlite_master", "staged"):
        with pytest.raises(ValueError):
            store.stage(conn, source, table, {"x": 1})


def test_reimporting_the_same_fact_does_not_duplicate_it(conn, source):
    first = store.stage(conn, source, "allergy", {"substance": "Penicillin"})
    second = store.stage(conn, source, "allergy", {"substance": " penicillin "})
    assert first is not None and second is None


def test_rows_without_a_name_field_do_not_collide(conn, source):
    """Two different readings must not look like the same fact."""
    first = store.stage(conn, source, "observation", {"panel": "a", "unit": "kg"})
    second = store.stage(conn, source, "observation", {"panel": "b", "unit": "kg"})
    assert first is not None and second is not None


def test_unknown_fields_are_kept_in_notes_not_dropped(conn, source):
    store.insert(conn, "allergy",
                 {"substance": "Latex", "reported_by": "Dr Moodley"})
    assert "reported_by=Dr Moodley" in store.rows(conn, "allergy")[0]["notes"]


def test_allergies_are_returned_worst_first(conn):
    for substance, severity in (("A", "mild"), ("B", "life-threatening"),
                                ("C", "moderate")):
        store.insert(conn, "allergy", {"substance": substance, "severity": severity})
    assert [r["substance"] for r in store.rows(conn, "allergy")] == ["B", "C", "A"]


def test_counts_include_the_pending_queue(conn, source):
    store.stage(conn, source, "condition", {"name": "Asthma"})
    counts = store.counts(conn)
    assert counts["pending"] == 1 and counts["condition"] == 0
