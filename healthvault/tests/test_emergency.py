"""The card is the safety-critical path: what it keeps under pressure,
and what it must never do."""

from datetime import date

import pytest

from healthvault import emergency, store

TODAY = date(2026, 8, 30)


def card_for(conn, **kwargs):
    return emergency.build(
        store.get_profile(conn),
        store.rows(conn, "allergy", "on_card = 1 AND status != 'resolved'"),
        store.rows(conn, "condition", "on_card = 1 AND status != 'resolved'"),
        store.rows(conn, "medication", "on_card = 1 AND status != 'stopped'"),
        store.rows(conn, "emergency_contact", "on_card = 1"),
        today=TODAY, **kwargs)


def test_card_carries_the_critical_facts(populated):
    text = card_for(populated).text
    assert "Aven Naidu" in text
    assert "Penicillin (anaphylaxis, life-threatening)" in text
    assert "Blood O+" in text
    assert "+27 82 555 0134" in text
    assert "ORGAN DONOR" in text


def test_age_is_derived_not_stored(populated):
    assert "(46)" in card_for(populated).text
    assert emergency.age_from("1980-05-02", TODAY) == 46
    assert emergency.age_from("not a date") is None


def test_resolved_items_are_excluded(populated):
    assert "Old fracture" not in card_for(populated).text


def test_allergies_are_ordered_worst_first(populated):
    text = card_for(populated).text
    assert text.index("Penicillin") < text.index("Latex")


def test_budget_drops_medications_before_allergies(populated):
    card = card_for(populated, budget=250)
    assert card.truncated
    assert "Penicillin" in card.text          # the thing that kills you stays
    assert "Metformin" not in card.text       # the rest gives way


def test_the_last_allergy_outranks_the_budget(populated):
    """A budget too small for everything must not delete the allergy.

    Going over budget costs a denser QR; dropping the line costs more.
    """
    card = card_for(populated, budget=80)
    assert "Penicillin (anaphylaxis, life-threatening)" in card.text
    assert card.truncated
    assert card.length > 80                   # deliberately over, and flagged


def test_budget_is_respected_when_it_can_be(populated):
    card = card_for(populated, budget=400)
    assert card.length <= 400
    assert not card.truncated


def test_identity_and_contact_survive_any_budget(populated):
    card = card_for(populated, budget=40)
    assert "Aven Naidu" in card.text
    assert "+27 82 555 0134" in card.text


def test_truncation_drops_whole_items_never_half_a_drug_name(populated):
    """A cut-off dose is a dangerous dose, so items go whole or not at all."""
    for budget in range(60, 400, 7):
        card = card_for(populated, budget=budget)
        for section in card.sections:
            for item in section.items:
                assert item in card.text


def test_dropped_items_are_counted_and_flagged(populated):
    card = card_for(populated, budget=250)
    assert card.dropped_total > 0
    assert "not shown)" in card.text or "more)" in card.text


def test_an_emptied_section_still_admits_it_existed(populated):
    """Omitting MEDS entirely would read as 'takes no medication'."""
    card = card_for(populated, budget=250)
    assert "Metformin" not in card.text
    assert "MEDS: (1 not shown)" in card.text


def test_a_genuinely_empty_section_stays_absent(populated):
    """Nothing recorded is different from something withheld."""
    card = card_for(populated, budget=400)
    assert "not shown" not in card.text
    assert "IMMUNISATIONS" not in card.text


def test_medical_aid_is_dropped_without_a_marker(populated):
    """Its absence carries no clinical meaning, so it costs no characters."""
    card = card_for(populated, budget=250)
    assert "MEDICAL AID" not in card.text


def test_empty_record_still_produces_a_usable_card(conn):
    card = card_for(conn)
    assert "EMERGENCY MEDICAL" in card.text
    assert "none recorded" in card.text       # explicit, not silently blank


def test_off_card_rows_are_never_encoded(populated):
    store.insert(populated, "condition",
                 dict(name="A private diagnosis", on_card=0))
    assert "private diagnosis" not in card_for(populated).text


@pytest.mark.parametrize("severity,rank_lower_than", [
    ("life-threatening", "severe"), ("severe", "moderate"), ("moderate", "mild")])
def test_severity_ordering(severity, rank_lower_than):
    assert emergency._severity_rank(severity) < emergency._severity_rank(rank_lower_than)
