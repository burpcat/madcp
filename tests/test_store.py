# tests/test_store.py
"""
Tests for the SQLite ticket store.
Stage 5: schema initialisation and list() interface only.
Stage 6 adds: CRUD round-trips, migration-on-read, failure note appending.

All tests use an in-memory SQLite database — no files written to disk.
"""

from __future__ import annotations

import sqlite3

import pytest

from madhu.store.sqlite import TicketStore, DB_SCHEMA_VERSION, ACTIVE_STATUSES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> TicketStore:
    """Fresh in-memory store for each test."""
    return TicketStore(":memory:")


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def test_init_creates_tickets_table(store: TicketStore) -> None:
    """The tickets table must exist after init_schema()."""
    with store._lock:
        cur = store._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets';"
        )
        assert cur.fetchone() is not None


def test_init_creates_touch_history_table(store: TicketStore) -> None:
    """The touch_history table must exist after init_schema()."""
    with store._lock:
        cur = store._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='touch_history';"
        )
        assert cur.fetchone() is not None


def test_init_creates_indexes(store: TicketStore) -> None:
    """All three indexes must be created."""
    expected = {
        "idx_tickets_status",
        "idx_tickets_tier",
        "idx_tickets_assigned",
    }
    with store._lock:
        cur = store._conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='index';")
        found = {row["name"] for row in cur.fetchall()}
    assert expected.issubset(found)


def test_init_stamps_schema_version(store: TicketStore) -> None:
    """user_version PRAGMA must be set to DB_SCHEMA_VERSION."""
    with store._lock:
        cur = store._conn.cursor()
        cur.execute("PRAGMA user_version;")
        version = cur.fetchone()[0]
    assert version == DB_SCHEMA_VERSION


def test_init_is_idempotent() -> None:
    """
    Calling init_schema() multiple times must not raise.
    IF NOT EXISTS guards all DDL — this confirms they're present.
    """
    store = TicketStore(":memory:")
    store.init_schema()   # second call
    store.init_schema()   # third call — must not raise


def test_store_as_context_manager() -> None:
    """TicketStore must work as a context manager and close cleanly."""
    with TicketStore(":memory:") as store:
        assert store._conn is not None
    # After exit, further queries should raise (connection closed)
    with pytest.raises(Exception):
        store._execute("SELECT 1;")


# ---------------------------------------------------------------------------
# list() — filter interface
# ---------------------------------------------------------------------------


def _insert_raw_ticket(
    store: TicketStore,
    ticket_id: str,
    status: str,
    tier_name: str = "Hamsa",
    assigned_to: str | None = None,
) -> None:
    """
    Insert a minimal raw ticket row directly via SQL.
    Used to set up list() filter tests without depending on stage 6 CRUD.
    """
    sql = """
        INSERT INTO tickets (
            id, schema_version, tier_name, tier_level, status,
            collaboration_mode, mtap, created_at, updated_at,
            created_by_agent, assigned_to_agent, payload_json,
            failure_notes_json
        ) VALUES (?, '1.0', ?, 24, ?, 'solo', 1,
                  '2024-01-01T00:00:00+00:00',
                  '2024-01-01T00:00:00+00:00',
                  'param-aatma', ?, '{}', '[]');
    """
    with store._lock:
        store._conn.execute(sql, (ticket_id, tier_name, status, assigned_to))
        store._conn.commit()


def test_list_returns_all_when_no_filters(store: TicketStore) -> None:
    """list() with no filters returns every ticket."""
    _insert_raw_ticket(store, "t-001", "queued")
    _insert_raw_ticket(store, "t-002", "done")
    rows = store.list()
    assert len(rows) == 2


def test_list_filter_by_single_status(store: TicketStore) -> None:
    """status= filters to tickets with exactly that status."""
    _insert_raw_ticket(store, "t-001", "queued")
    _insert_raw_ticket(store, "t-002", "done")
    rows = store.list(status="queued")
    assert len(rows) == 1
    assert rows[0]["status"] == "queued"


def test_list_filter_by_status_in(store: TicketStore) -> None:
    """
    status_in= filters to tickets whose status is in the provided set.
    This is the interface required by the naming service (stage 4).
    """
    _insert_raw_ticket(store, "t-001", "queued")
    _insert_raw_ticket(store, "t-002", "in_progress")
    _insert_raw_ticket(store, "t-003", "done")
    rows = store.list(status_in={"queued", "in_progress"})
    assert len(rows) == 2
    statuses = {r["status"] for r in rows}
    assert statuses == {"queued", "in_progress"}


def test_list_filter_by_tier(store: TicketStore) -> None:
    """tier= filters to tickets at the named tier."""
    _insert_raw_ticket(store, "t-001", "queued", tier_name="Hamsa")
    _insert_raw_ticket(store, "t-002", "queued", tier_name="Adi Purusha")
    rows = store.list(tier="Hamsa")
    assert len(rows) == 1
    assert rows[0]["tier_name"] == "Hamsa"


def test_list_filter_by_assigned_to(store: TicketStore) -> None:
    """assigned_to= filters to tickets assigned to the named agent."""
    _insert_raw_ticket(store, "t-001", "in_progress", assigned_to="vasishtha")
    _insert_raw_ticket(store, "t-002", "in_progress", assigned_to="atri")
    rows = store.list(assigned_to="vasishtha")
    assert len(rows) == 1
    assert rows[0]["assigned_to_agent"] == "vasishtha"


def test_list_combined_filters(store: TicketStore) -> None:
    """Multiple filters are ANDed together."""
    _insert_raw_ticket(store, "t-001", "queued",      tier_name="Hamsa")
    _insert_raw_ticket(store, "t-002", "in_progress", tier_name="Hamsa")
    _insert_raw_ticket(store, "t-003", "queued",      tier_name="Adi Purusha")
    rows = store.list(status="queued", tier="Hamsa")
    assert len(rows) == 1
    assert rows[0]["id"] == "t-001"


def test_list_status_in_takes_precedence_over_status(store: TicketStore) -> None:
    """When both status and status_in are provided, status_in wins."""
    _insert_raw_ticket(store, "t-001", "queued")
    _insert_raw_ticket(store, "t-002", "done")
    # status="done" would return only t-002, but status_in overrides
    rows = store.list(status="done", status_in={"queued"})
    assert len(rows) == 1
    assert rows[0]["id"] == "t-001"


def test_active_statuses_constant(store: TicketStore) -> None:
    """
    ACTIVE_STATUSES must contain exactly the three transitional states.
    This is the set the naming service uses to determine name availability.
    """
    assert ACTIVE_STATUSES == {"queued", "touched", "in_progress"}


def test_list_empty_returns_empty_list(store: TicketStore) -> None:
    """list() on an empty store returns an empty list, not None."""
    rows = store.list()
    assert rows == []
