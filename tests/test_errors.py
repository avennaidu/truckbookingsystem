from truckbot.errors import RETRY, SKIP, classify_error


def test_import_release_hold_is_permanent_skip():
    kind, why = classify_error(
        "Application Error: create failed !IMPORT RELEASE")
    assert kind == SKIP and "hold" in why


def test_already_has_appointment_variants_skip():
    for txt in ("Unit already has an appointment",
                "An appointment exists for this unit",
                "duplicate appointment"):
        assert classify_error(txt)[0] == SKIP


def test_no_openings_is_retry():
    assert classify_error("No Appointment Openings Available") == \
        (RETRY, "no openings")


def test_unknown_error_is_retry_not_skip():
    # regression: the prototype permanently skipped PIDU4403347 on a
    # transient "the create operations ha..." server error
    kind, why = classify_error(
        "clusternode10 | ictsi/za/dgt/dgt/trk-smacala | v 4.0.31\n"
        "the create operations has failed unexpectedly")
    assert kind == RETRY
    assert "create operations" in why


def test_empty_text_is_retry():
    assert classify_error("") == (RETRY, "unknown error")
    assert classify_error(None) == (RETRY, "unknown error")
