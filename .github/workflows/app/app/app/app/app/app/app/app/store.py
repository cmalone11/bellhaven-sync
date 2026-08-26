"""
Local state store for proposals.

This is the piece that makes daily reruns safe. Every proposal the matcher
generates has a deterministic `dedupe_key` (see matcher.py). Before inserting
a freshly generated proposal we check whether that key already exists:

  - If it exists and is still `pending`     -> skip (don't spam duplicates)
  - If it exists and was `approved`/`rejected` -> skip (already decided,
    never resurrect a decided item just because the source data still
    matches the same way)
  - If it does not exist -> insert as `pending`

If the underlying facts change (e.g. the website now shows a different city
for the same slug), the matcher produces a *new* dedupe_key, so the old,
now-stale proposal simply stops being regenerated and ages out while a fresh
one is queued for review. Nothing is silently overwritten.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT UNIQUE NOT NULL,
    kind          TEXT NOT NULL,      -- confident_match | needs_fix | new_location |
                                       -- orphaned_crm_account | duplicate | chow
    summary       TEXT NOT NULL,      -- short human-readable title
    evidence_json TEXT NOT NULL,      -- everything the reviewer needs to see
    actions_json  TEXT NOT NULL,      -- the literal API call(s) to perform on approval
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at    REAL NOT NULL,
    decided_at    REAL,
    decided_by    TEXT,
    result_json   TEXT               -- API response(s) captured after apply, for audit
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute(SCHEMA)


def upsert_proposal(dedupe_key, kind, summary, evidence: dict, actions: list) -> str:
    """Insert if new. Returns 'inserted', 'already_pending', or 'already_decided'."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        if row:
            return "already_pending" if row["status"] == "pending" else "already_decided"
        conn.execute(
            "INSERT INTO proposals (dedupe_key, kind, summary, evidence_json, actions_json,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (dedupe_key, kind, summary, json.dumps(evidence), json.dumps(actions), time.time()),
        )
        return "inserted"


def list_proposals(status: str | None = None) -> list[dict]:
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM proposals ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_proposal(proposal_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return dict(row) if row else None


def decide_proposal(proposal_id: int, status: str, decided_by: str, result: dict | None = None):
    with _conn() as conn:
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?, result_json = ?"
            " WHERE id = ?",
            (status, time.time(), decided_by, json.dumps(result) if result else None, proposal_id),
        )
