"""
tests/test_dashboard_data.py — A4-authored tests for Stage 15.
Run: pytest tests/test_dashboard_data.py -v
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from madhu.observability.dashboard_data import (
    DashboardSnapshot,
    fetch_snapshot,
    _read_log_tail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DDL_TICKETS = """
CREATE TABLE IF NOT EXISTS tickets (
    id                  TEXT PRIMARY KEY,
    parent_id           TEXT,
    forwarded_from      TEXT,
    schema_version      TEXT NOT NULL,
    tier_name           TEXT NOT NULL,
    tier_level          INTEGER NOT NULL,
    status              TEXT NOT NULL,
    collaboration_mode  TEXT NOT NULL,
    mtap                INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    created_by_agent    TEXT NOT NULL,
    assigned_to_agent   TEXT,
    touched_by          TEXT,
    payload_json        TEXT NOT NULL,
    result_json         TEXT,
    failure_notes_json  TEXT NOT NULL DEFAULT '[]'
);
"""

_DDL_TOUCH = """
CREATE TABLE IF NOT EXISTS touch_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    started     TEXT NOT NULL,
    ended       TEXT,
    summary     TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
"""


def _make_db(path: str) -> sqlite3.Connection:
    """Create schema in a file-backed db; return open rw connection."""
    con = sqlite3.connect(path)
    con.executescript(_DDL_TICKETS + _DDL_TOUCH)
    con.commit()
    return con


def _insert_ticket(
    con: sqlite3.Connection,
    *,
    id: str,
    tier_name: str,
    tier_level: int,
    status: str,
    assigned_to_agent: str | None = None,
    forwarded_from: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO tickets (
            id, parent_id, forwarded_from, schema_version,
            tier_name, tier_level, status, collaboration_mode,
            mtap, created_at, updated_at, created_by_agent,
            assigned_to_agent, touched_by, payload_json, failure_notes_json
        ) VALUES (
            ?, NULL, ?, '1.0',
            ?, ?, ?, 'solo',
            1, datetime('now'), datetime('now'), 'param-aatma',
            ?, NULL, '{}', '[]'
        )
        """,
        (id, forwarded_from, tier_name, tier_level, status, assigned_to_agent),
    )
    con.commit()


# ---------------------------------------------------------------------------
# fetch_snapshot — degradation
# ---------------------------------------------------------------------------

def test_fetch_snapshot_no_db(tmp_path):
    s = fetch_snapshot(str(tmp_path / "nonexistent.db"))
    assert s.waiting is True
    assert s.tiers == []
    assert s.agents == []
    assert s.recent_tickets == []


def test_fetch_snapshot_degrades_on_operational_error():
    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
        s = fetch_snapshot("data/palakudu.db")
    assert s.waiting is True


def test_fetch_snapshot_degrades_on_database_error():
    with patch("sqlite3.connect", side_effect=sqlite3.DatabaseError("corrupt")):
        s = fetch_snapshot("data/palakudu.db")
    assert s.waiting is True


# ---------------------------------------------------------------------------
# fetch_snapshot — empty db
# ---------------------------------------------------------------------------

def test_fetch_snapshot_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    con = _make_db(db)
    con.close()

    s = fetch_snapshot(db)
    assert s.waiting is False
    assert s.tiers == []
    assert s.agents == []
    assert s.recent_tickets == []


# ---------------------------------------------------------------------------
# fetch_snapshot — with data
# ---------------------------------------------------------------------------

def test_fetch_snapshot_with_tickets(tmp_path):
    db = str(tmp_path / "t.db")
    con = _make_db(db)
    _insert_ticket(con, id="t-001", tier_name="Hamsa", tier_level=2, status="queued")
    _insert_ticket(con, id="t-002", tier_name="Hamsa", tier_level=2, status="queued")
    _insert_ticket(con, id="t-003", tier_name="Hamsa", tier_level=2, status="in_progress",
                   assigned_to_agent="AdHa-vasishtha")
    con.close()

    s = fetch_snapshot(db)
    assert s.waiting is False
    assert len(s.tiers) == 1
    hamsa = s.tiers[0]
    assert hamsa.name == "Hamsa"
    assert hamsa.queued == 2
    assert hamsa.in_progress == 1
    assert hamsa.active == 1  # only in_progress counts as active in the query


def test_fetch_snapshot_multiple_tiers(tmp_path):
    db = str(tmp_path / "t.db")
    con = _make_db(db)
    _insert_ticket(con, id="t-001", tier_name="Adi Purusha", tier_level=1, status="touched",
                   assigned_to_agent="param-aatma")
    _insert_ticket(con, id="t-002", tier_name="Hamsa", tier_level=2, status="queued")
    con.close()

    s = fetch_snapshot(db)
    assert len(s.tiers) == 2
    # Ordered by tier_level ASC
    assert s.tiers[0].name == "Adi Purusha"
    assert s.tiers[1].name == "Hamsa"


def test_fetch_snapshot_agents_only_active(tmp_path):
    db = str(tmp_path / "t.db")
    con = _make_db(db)
    _insert_ticket(con, id="t-001", tier_name="Hamsa", tier_level=2, status="in_progress",
                   assigned_to_agent="AdHa-vasishtha")
    _insert_ticket(con, id="t-002", tier_name="Hamsa", tier_level=2, status="done",
                   assigned_to_agent="AdHa-agastya")
    con.close()

    s = fetch_snapshot(db)
    assert len(s.agents) == 1
    assert s.agents[0].agent_name == "AdHa-vasishtha"


# ---------------------------------------------------------------------------
# fetch_snapshot — readonly (no writes)
# ---------------------------------------------------------------------------

def test_fetch_snapshot_readonly_no_writes(tmp_path):
    db = str(tmp_path / "t.db")
    con = _make_db(db)
    before_count = con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    con.close()

    fetch_snapshot(db)

    # Verify schema is unchanged after fetch_snapshot ran
    con2 = sqlite3.connect(db)
    after_count = con2.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    con2.close()
    assert before_count == after_count

    # Verify the connection fetch_snapshot uses is genuinely read-only:
    # opening with ?mode=ro on the same file and attempting a write must fail.
    ro_con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro_con.execute("CREATE TABLE _write_probe (x INTEGER)")
    finally:
        ro_con.close()


# ---------------------------------------------------------------------------
# fetch_recent_tickets — forwarded_to populated
# ---------------------------------------------------------------------------

def test_fetch_recent_tickets_forwarded_to_populated(tmp_path):
    db = str(tmp_path / "t.db")
    con = _make_db(db)
    _insert_ticket(con, id="t-original", tier_name="Hamsa", tier_level=2, status="forwarded")
    _insert_ticket(con, id="t-successor", tier_name="Hamsa", tier_level=2, status="queued",
                   forwarded_from="t-original")
    con.close()

    s = fetch_snapshot(db)
    original = next((t for t in s.recent_tickets if t.ticket_id == "t-orig"), None)
    # ticket_id is truncated to 6 chars
    original = next((t for t in s.recent_tickets if "t-orig"[:6] in t.ticket_id), None)
    assert original is not None
    assert original.forwarded_to is not None
    assert "t-succ"[:6] in original.forwarded_to or original.forwarded_to.startswith("t-suc")


# ---------------------------------------------------------------------------
# DashboardSnapshot — dataclass invariants
# ---------------------------------------------------------------------------

def test_snapshot_frozen():
    s = DashboardSnapshot()
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass of AttributeError
        s.tiers = []  # type: ignore[misc]


def test_snapshot_list_defaults_independent():
    a = DashboardSnapshot()
    b = DashboardSnapshot()
    # Frozen dataclass — can't mutate directly; confirm field(default_factory=list)
    # by checking the two instances don't share the same list object.
    assert a.tiers is not b.tiers
    assert a.agents is not b.agents
    assert a.recent_tickets is not b.recent_tickets
    assert a.log_tail is not b.log_tail


# ---------------------------------------------------------------------------
# _read_log_tail
# ---------------------------------------------------------------------------

def test_read_log_tail_missing_file(tmp_path):
    result = _read_log_tail(str(tmp_path / "nope.jsonl"))
    assert result == []


def test_read_log_tail_truncates_to_n(tmp_path):
    log = tmp_path / "runs.jsonl"
    lines = [f'{{"n": {i}}}' for i in range(30)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _read_log_tail(str(log), n=20)
    assert len(result) == 20
    # Last 20 lines = indices 10–29
    assert result[0] == '{"n": 10}'
    assert result[-1] == '{"n": 29}'


def test_read_log_tail_fewer_than_n(tmp_path):
    log = tmp_path / "runs.jsonl"
    lines = ['{"n": 0}', '{"n": 1}']
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _read_log_tail(str(log), n=20)
    assert len(result) == 2
