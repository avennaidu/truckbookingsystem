"""SQLite schema and connection handling.

One file holds the whole record (default `healthvault.db`). The schema is
created on demand and versioned in `meta.schema_version`, so opening an
older file upgrades it in place.

Note on encryption: plain SQLite is not encrypted. Protecting the file at
rest is the operating system's job (BitLocker / FileVault / LUKS) - see
`crypto.py` for the encrypted *export* path, which is the part that
travels. The README says this plainly rather than implying more.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_DB = Path("healthvault.db")

#: Every clinical table carries these provenance columns.
_PROVENANCE = """
    source_id   INTEGER REFERENCES source(id),
    created_at  TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Where a fact came from. Written by every importer, and by manual edits.
CREATE TABLE IF NOT EXISTS source (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- manual|email|medical_aid|fhir|apple_health|csv|pdf
    label       TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{{}}'
);

-- Exactly one row. `id` is pinned to 1 so it cannot be duplicated.
CREATE TABLE IF NOT EXISTS profile (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    full_name      TEXT NOT NULL DEFAULT '',
    preferred_name TEXT NOT NULL DEFAULT '',
    dob            TEXT NOT NULL DEFAULT '',
    sex            TEXT NOT NULL DEFAULT '',
    blood_type     TEXT NOT NULL DEFAULT '',
    id_number      TEXT NOT NULL DEFAULT '',
    organ_donor    INTEGER NOT NULL DEFAULT 0,
    scheme         TEXT NOT NULL DEFAULT '',
    plan           TEXT NOT NULL DEFAULT '',
    member_number  TEXT NOT NULL DEFAULT '',
    dependant_code TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS emergency_contact (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    alt_phone    TEXT NOT NULL DEFAULT '',
    priority     INTEGER NOT NULL DEFAULT 1,
    on_card      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS allergy (
    id        INTEGER PRIMARY KEY,
    substance TEXT NOT NULL,
    reaction  TEXT NOT NULL DEFAULT '',
    severity  TEXT NOT NULL DEFAULT '',
    onset     TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'active',
    on_card   INTEGER NOT NULL DEFAULT 1,     -- allergies default ON
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS condition (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL DEFAULT '',
    code_system TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    onset       TEXT NOT NULL DEFAULT '',
    resolved    TEXT NOT NULL DEFAULT '',
    severity    TEXT NOT NULL DEFAULT '',
    on_card     INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS medication (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    dose       TEXT NOT NULL DEFAULT '',
    frequency  TEXT NOT NULL DEFAULT '',
    route      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    prescriber TEXT NOT NULL DEFAULT '',
    started    TEXT NOT NULL DEFAULT '',
    stopped    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',
    on_card    INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS immunisation (
    id       INTEGER PRIMARY KEY,
    vaccine  TEXT NOT NULL,
    date     TEXT NOT NULL DEFAULT '',
    batch    TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    on_card  INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS procedure (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    date     TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    facility TEXT NOT NULL DEFAULT '',
    on_card  INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS observation (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    value     TEXT NOT NULL DEFAULT '',
    unit      TEXT NOT NULL DEFAULT '',
    ref_range TEXT NOT NULL DEFAULT '',
    abnormal  INTEGER NOT NULL DEFAULT 0,
    date      TEXT NOT NULL DEFAULT '',
    panel     TEXT NOT NULL DEFAULT '',
    on_card   INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS encounter (
    id       INTEGER PRIMARY KEY,
    date     TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT '',      -- consult|admission|casualty|procedure|pharmacy
    provider TEXT NOT NULL DEFAULT '',
    facility TEXT NOT NULL DEFAULT '',
    reason   TEXT NOT NULL DEFAULT '',
    summary  TEXT NOT NULL DEFAULT '',
    cost     TEXT NOT NULL DEFAULT '',
    on_card  INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS document (
    id       INTEGER PRIMARY KEY,
    title    TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT '',      -- lab_result|prescription|referral|discharge|imaging|invoice
    date     TEXT NOT NULL DEFAULT '',
    path     TEXT NOT NULL DEFAULT '',
    sha256   TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT '',
    on_card  INTEGER NOT NULL DEFAULT 0,
    {_PROVENANCE}
);

CREATE TABLE IF NOT EXISTS provider (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    speciality TEXT NOT NULL DEFAULT '',
    practice_number TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    UNIQUE (name, practice_number)
);

-- The review queue. Importers write ONLY here; approving copies the row
-- into its real table. `dedup_key` stops a re-import queueing twins.
CREATE TABLE IF NOT EXISTS staged (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES source(id),
    table_name TEXT NOT NULL,
    payload    TEXT NOT NULL,               -- json of the row-to-be
    confidence REAL NOT NULL DEFAULT 0.5,
    reason     TEXT NOT NULL DEFAULT '',    -- why the importer thinks this
    dedup_key  TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected
    created_at TEXT NOT NULL,
    UNIQUE (table_name, dedup_key)
);

CREATE TABLE IF NOT EXISTS share (
    id         INTEGER PRIMARY KEY,
    token      TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT 'summary',  -- see share.SCOPES
    pin_hash   TEXT NOT NULL DEFAULT '',
    pin_salt   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    max_views  INTEGER NOT NULL DEFAULT 0,       -- 0 = unlimited
    views      INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS share_access (
    id       INTEGER PRIMARY KEY,
    share_id INTEGER NOT NULL REFERENCES share(id),
    at       TEXT NOT NULL,
    ip       TEXT NOT NULL DEFAULT '',
    ok       INTEGER NOT NULL DEFAULT 1,
    note     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_staged_status ON staged(status);
CREATE INDEX IF NOT EXISTS idx_share_token   ON share(token);
CREATE INDEX IF NOT EXISTS idx_access_share  ON share_access(share_id);
"""


def now() -> str:
    """UTC timestamp, seconds precision - the format stored everywhere."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the record file and return a connection."""
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    conn.execute(
        "INSERT OR IGNORE INTO profile (id, updated_at) VALUES (1, ?)", (now(),)
    )
    conn.commit()


def add_source(conn: sqlite3.Connection, kind: str, label: str, **detail) -> int:
    """Record an import and return its id, for stamping onto rows."""
    cur = conn.execute(
        "INSERT INTO source (kind, label, imported_at, detail) VALUES (?, ?, ?, ?)",
        (kind, label, now(), json.dumps(detail, default=str)),
    )
    conn.commit()
    return int(cur.lastrowid)
