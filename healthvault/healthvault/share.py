"""Practitioner shares: expiring, revocable, logged links to the record.

The threat this guards against is mundane rather than exotic. You show a
QR to a receptionist; the link ends up in a browser history, a WhatsApp
thread, a screenshot. So a share is:

* SCOPED - a locum needs meds and allergies, not twenty years of claims;
* SHORT-LIVED - it expires by default in a day, not never;
* OPTIONALLY PIN-LOCKED - a 4-8 digit PIN you say out loud, which stops
  the link alone being enough;
* VIEW-CAPPED - `max_views=1` makes it single-use;
* REVOCABLE - one click kills it;
* LOGGED - every hit, allowed or refused, is recorded with a timestamp.

PINs are stored with `hashlib.scrypt` and a per-share salt. A PIN is
short and therefore weak on its own; the view cap and expiry are what
make it adequate, and `check` deliberately fails the same way for a
wrong PIN as for an unknown token so a scanner cannot enumerate shares.
"""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import now

#: What a share may expose. Keep the default narrow.
SCOPES = {
    "emergency": ("Emergency card only",
                  ("allergy", "condition", "medication")),
    "summary":   ("Summary - allergies, conditions, meds, immunisations",
                  ("allergy", "condition", "medication", "immunisation")),
    "clinical":  ("Clinical - summary plus results, procedures and visits",
                  ("allergy", "condition", "medication", "immunisation",
                   "observation", "procedure", "encounter")),
    "full":      ("Everything, including documents",
                  ("allergy", "condition", "medication", "immunisation",
                   "observation", "procedure", "encounter", "document")),
}

DEFAULT_SCOPE = "summary"
DEFAULT_HOURS = 24

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


@dataclass
class Decision:
    ok: bool
    reason: str          # ok|expired|revoked|exhausted|denied|needs_pin
    message: str


def hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, **_SCRYPT)
    return digest.hex(), salt.hex()


def verify_pin(pin: str, pin_hash: str, pin_salt: str) -> bool:
    if not pin_hash:
        return True
    try:
        salt = bytes.fromhex(pin_salt)
    except ValueError:
        return False
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(digest.hex(), pin_hash)


def new_token() -> str:
    """128 bits of URL-safe randomness - not guessable, still scannable."""
    return secrets.token_urlsafe(16)


def create(conn: sqlite3.Connection, label: str = "", scope: str = DEFAULT_SCOPE,
           hours: int = DEFAULT_HOURS, pin: str = "", max_views: int = 0) -> str:
    """Mint a share and return its token."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; pick one of {sorted(SCOPES)}")
    token = new_token()
    pin_hash, pin_salt = hash_pin(pin) if pin else ("", "")
    expires = ""
    if hours > 0:
        expires = (datetime.now(timezone.utc).replace(microsecond=0)
                   + timedelta(hours=hours)).isoformat()
    conn.execute(
        "INSERT INTO share (token, label, scope, pin_hash, pin_salt,"
        " created_at, expires_at, max_views) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, label, scope, pin_hash, pin_salt, now(), expires, max_views),
    )
    conn.commit()
    return token


def find(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM share WHERE token = ?", (token,)).fetchone()


def check(row: sqlite3.Row | None, pin: str = "",
          at: datetime | None = None) -> Decision:
    """Decide whether this request may see the record.

    An unknown token and a wrong PIN both return `denied` with identical
    wording, so a scanner learns nothing from the difference.
    """
    denied = Decision(False, "denied", "This link is not valid.")
    if row is None:
        return denied
    if row["revoked"]:
        return Decision(False, "revoked", "This link has been revoked.")
    if row["expires_at"]:
        at = at or datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return denied
        if at >= expires:
            return Decision(False, "expired", "This link has expired.")
    if row["max_views"] and row["views"] >= row["max_views"]:
        return Decision(False, "exhausted", "This link has already been used.")
    if row["pin_hash"]:
        if not pin:
            return Decision(False, "needs_pin", "Enter the PIN you were given.")
        if not verify_pin(pin, row["pin_hash"], row["pin_salt"]):
            return denied
    return Decision(True, "ok", "")


def record_access(conn: sqlite3.Connection, share_id: int, ip: str,
                  ok: bool, note: str = "") -> None:
    conn.execute(
        "INSERT INTO share_access (share_id, at, ip, ok, note) VALUES (?, ?, ?, ?, ?)",
        (share_id, now(), ip, int(ok), note))
    if ok:
        conn.execute("UPDATE share SET views = views + 1 WHERE id = ?", (share_id,))
    conn.commit()


def revoke(conn: sqlite3.Connection, token: str) -> bool:
    cur = conn.execute("UPDATE share SET revoked = 1 WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount > 0


def revoke_all(conn: sqlite3.Connection) -> int:
    cur = conn.execute("UPDATE share SET revoked = 1 WHERE revoked = 0")
    conn.commit()
    return cur.rowcount


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM share ORDER BY created_at DESC"))


def access_log(conn: sqlite3.Connection, share_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM share_access WHERE share_id = ? "
        "ORDER BY at DESC, id DESC",
        (share_id,)))


def tables_for(scope: str) -> tuple[str, ...]:
    return SCOPES.get(scope, SCOPES[DEFAULT_SCOPE])[1]
