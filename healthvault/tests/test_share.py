"""Share links: every way in, and every way they are refused."""

from datetime import datetime, timedelta, timezone

import pytest

from healthvault import share


def make(conn, **kwargs):
    token = share.create(conn, **kwargs)
    return token, share.find(conn, token)


def test_valid_share_opens(conn):
    _, row = make(conn, label="Dr Moodley", scope="clinical")
    assert share.check(row).ok


def test_unknown_token_is_denied(conn):
    assert share.check(share.find(conn, "nope")).reason == "denied"


def test_pin_is_required_then_accepted(conn):
    _, row = make(conn, pin="4821")
    assert share.check(row).reason == "needs_pin"
    assert share.check(row, "4821").ok


def test_wrong_pin_and_unknown_token_are_indistinguishable(conn):
    """A scanner must not learn that a token exists by probing PINs."""
    _, row = make(conn, pin="4821")
    wrong = share.check(row, "0000")
    unknown = share.check(None)
    assert (wrong.reason, wrong.message) == (unknown.reason, unknown.message)


def test_expiry(conn):
    _, row = make(conn, hours=24)
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert share.check(row, at=later).reason == "expired"
    sooner = datetime.now(timezone.utc) + timedelta(hours=1)
    assert share.check(row, at=sooner).ok


def test_never_expires_when_hours_is_zero(conn):
    _, row = make(conn, hours=0)
    far = datetime.now(timezone.utc) + timedelta(days=3650)
    assert share.check(row, at=far).ok


def test_view_cap_exhausts(conn):
    token, row = make(conn, max_views=1)
    share.record_access(conn, row["id"], "10.0.0.1", True)
    assert share.check(share.find(conn, token)).reason == "exhausted"


def test_refused_access_does_not_burn_a_view(conn):
    token, row = make(conn, max_views=1, pin="1234")
    share.record_access(conn, row["id"], "10.0.0.1", False, "denied")
    assert share.find(conn, token)["views"] == 0


def test_revoke(conn):
    token, _ = make(conn)
    assert share.revoke(conn, token)
    assert share.check(share.find(conn, token)).reason == "revoked"


def test_revoke_all(conn):
    for _ in range(3):
        make(conn)
    assert share.revoke_all(conn) == 3
    assert all(r["revoked"] for r in share.active(conn))


def test_every_attempt_is_logged(conn):
    token, row = make(conn, pin="4821")
    share.record_access(conn, row["id"], "10.0.0.5", False, "needs_pin")
    share.record_access(conn, row["id"], "10.0.0.5", True, "ok")
    log = share.access_log(conn, row["id"])
    assert len(log) == 2
    assert [entry["ok"] for entry in log] == [1, 0]      # newest first


def test_tokens_are_unique_and_unguessable(conn):
    tokens = {share.create(conn) for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 20 for t in tokens)


def test_scope_limits_what_is_shown():
    assert "document" not in share.tables_for("summary")
    assert "document" in share.tables_for("full")
    assert "observation" not in share.tables_for("emergency")


def test_unknown_scope_is_rejected(conn):
    with pytest.raises(ValueError):
        share.create(conn, scope="everything")


def test_pin_hashing_is_salted():
    first, salt_a = share.hash_pin("1234")
    second, salt_b = share.hash_pin("1234")
    assert first != second and salt_a != salt_b
    assert share.verify_pin("1234", first, salt_a)
    assert not share.verify_pin("4321", first, salt_a)
