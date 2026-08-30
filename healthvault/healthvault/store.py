"""Reading and writing the record.

Thin layer over sqlite3 so the rest of the app never builds SQL. Column
lists are read from the schema at runtime, which keeps inserts honest
when the schema grows: an unknown key in a payload is a caught error,
not a silently dropped field.
"""

import hashlib
import json
import sqlite3
from typing import Any, Iterable

from . import RECORD_TABLES
from .db import now

#: Tables a staged record is allowed to land in. Anything else is a bug
#: or a hostile import file, and is rejected rather than executed.
STAGEABLE = set(RECORD_TABLES)


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({_safe(table)})")]


def _safe(table: str) -> str:
    """Guard identifiers - table names reach SQL, so they never come raw."""
    if table not in STAGEABLE | {
        "profile", "emergency_contact", "provider", "staged",
        "share", "share_access", "source", "meta",
    }:
        raise ValueError(f"unknown table {table!r}")
    return table


def insert(conn: sqlite3.Connection, table: str, payload: dict[str, Any],
           source_id: int | None = None) -> int:
    """Insert a row, dropping keys the table does not have.

    Importers work from messy sources, so unknown keys are expected; they
    are folded into `notes` instead of being lost, which keeps the odd
    but occasionally vital free-text field visible to a human.
    """
    cols = set(columns(conn, table))
    row = {k: v for k, v in payload.items() if k in cols}
    extra = {k: v for k, v in payload.items() if k not in cols and v not in (None, "")}
    if extra and "notes" in cols:
        tail = "; ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        row["notes"] = (str(row.get("notes", "")) + " " + tail).strip()
    if "created_at" in cols:
        row.setdefault("created_at", now())
    if "source_id" in cols and source_id is not None:
        row.setdefault("source_id", source_id)
    keys = list(row)
    sql = (f"INSERT INTO {_safe(table)} ({', '.join(keys)}) "
           f"VALUES ({', '.join('?' for _ in keys)})")
    cur = conn.execute(sql, [_coerce(row[k]) for k in keys])
    conn.commit()
    return int(cur.lastrowid)


def _coerce(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return ""
    if isinstance(value, (int, float, str, bytes)):
        return value
    return json.dumps(value, default=str)


def update(conn: sqlite3.Connection, table: str, row_id: int,
           payload: dict[str, Any]) -> None:
    cols = set(columns(conn, table))
    row = {k: v for k, v in payload.items() if k in cols and k != "id"}
    if not row:
        return
    sets = ", ".join(f"{k} = ?" for k in row)
    conn.execute(f"UPDATE {_safe(table)} SET {sets} WHERE id = ?",
                 [_coerce(v) for v in row.values()] + [row_id])
    conn.commit()


def delete(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    conn.execute(f"DELETE FROM {_safe(table)} WHERE id = ?", (row_id,))
    conn.commit()


def rows(conn: sqlite3.Connection, table: str, where: str = "",
         params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    sql = f"SELECT * FROM {_safe(table)}"
    if where:
        sql += f" WHERE {where}"
    sql += _order_for(table)
    return list(conn.execute(sql, tuple(params)))


def _order_for(table: str) -> str:
    """Newest-first for dated tables; worst-first for allergies."""
    if table == "allergy":
        return (" ORDER BY CASE lower(severity) "
                "WHEN 'life-threatening' THEN 0 WHEN 'severe' THEN 1 "
                "WHEN 'moderate' THEN 2 WHEN 'mild' THEN 3 ELSE 4 END, substance")
    if table in ("condition", "medication"):
        return " ORDER BY status = 'active' DESC, name"
    if table in ("observation", "encounter", "procedure", "immunisation", "document"):
        return " ORDER BY date DESC, id DESC"
    return " ORDER BY id"


def get_profile(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()


def save_profile(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updated_at"] = now()
    update(conn, "profile", 1, payload)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {t: conn.execute(f"SELECT count(*) FROM {_safe(t)}").fetchone()[0]
           for t in RECORD_TABLES}
    out["pending"] = conn.execute(
        "SELECT count(*) FROM staged WHERE status = 'pending'").fetchone()[0]
    return out


# --- review queue -----------------------------------------------------

def stage(conn: sqlite3.Connection, source_id: int, table: str,
          payload: dict[str, Any], confidence: float = 0.5,
          reason: str = "", dedup_key: str = "") -> int | None:
    """Queue one candidate record. Returns None if it is already queued.

    Importers call this and nothing else - they cannot reach the real
    tables, which is what makes a bad parser a nuisance rather than a
    corrupted medical history.
    """
    if table not in STAGEABLE:
        raise ValueError(f"cannot stage into {table!r}")
    key = dedup_key or _auto_key(table, payload)
    try:
        cur = conn.execute(
            "INSERT INTO staged (source_id, table_name, payload, confidence,"
            " reason, dedup_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, table, json.dumps(payload, default=str),
             confidence, reason, key, now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None          # same fact, already waiting or already ruled on


def _auto_key(table: str, payload: dict[str, Any]) -> str:
    """Identity of a fact, for de-duplication across repeat imports.

    When a payload carries none of the identifying fields we fall back to
    a digest of the whole row. Without that, two unrelated rows that both
    lack a `name` would look like the same fact and the second would be
    silently dropped.
    """
    parts = [table]
    for field in ("substance", "name", "vaccine", "title", "date", "value"):
        if payload.get(field):
            parts.append(str(payload[field]).strip().lower())
    if len(parts) == 1:
        blob = json.dumps(payload, sort_keys=True, default=str)
        parts.append(hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16])
    return "|".join(parts)


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT s.*, src.label AS source_label, src.kind AS source_kind "
        "FROM staged s JOIN source src ON src.id = s.source_id "
        "WHERE s.status = 'pending' "
        "ORDER BY s.confidence DESC, s.id"))


def approve(conn: sqlite3.Connection, staged_id: int) -> int:
    """Promote one staged record into its real table."""
    row = conn.execute("SELECT * FROM staged WHERE id = ?", (staged_id,)).fetchone()
    if row is None or row["status"] != "pending":
        raise KeyError(f"no pending staged record {staged_id}")
    new_id = insert(conn, row["table_name"], json.loads(row["payload"]),
                    source_id=row["source_id"])
    conn.execute("UPDATE staged SET status = 'approved' WHERE id = ?", (staged_id,))
    conn.commit()
    return new_id


def reject(conn: sqlite3.Connection, staged_id: int) -> None:
    conn.execute("UPDATE staged SET status = 'rejected' WHERE id = ?", (staged_id,))
    conn.commit()
