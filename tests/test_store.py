# tests/test_store.py
"""
Tests for the SQLite ticket store.
Stage 5: schema initialisation and list() interface.
Stage 6: CRUD round-trips, migrate-on-read, failure note appending,
         touch entry insertion, list() returning Ticket objects.

All tests use an in-memory SQLite database — no files written to disk.

Note: several tests access store._conn and store._lock directly.
This coupling to private internals is intentional for a storage layer
test — internal schema state is exactly what these tests verify.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from madhu.schemas.envelope import Envelope, FailureNote, Ticket, TouchEntry
from madhu.store.sqlite import TicketStore, DB_SCHEMA_VERSION, ACTIVE_STATUSES

# Hamsa is tier level 24 (last in KRISHNAS).
# Derived here so tests don't hardcode the magic number.
from madhu.names import KRISHNAS
HAMSA_LEVEL = KRISHNAS.index("Hamsa") + 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> TicketStore:
    """Fresh in-memory store for each test."""
    return TicketStore(":memory:")


def make_ticket(
    tier_name: str = "Hamsa",
    status: str = "queued",
    assigned_to: str | None = None,
) -> Ticket:
    """Build a minimal valid Ticket for testing."""
    env = Envelope(
        tier_name=tier_name,
        tier_level=HAMSA_LEVEL,
        status=status,
        assigned_to_agent=assigned_to,
    )
    return Ticket(
        envelope=env,
        payload={"type": "function_spec", "name": "smoke"},
    )


# ---------------------------------------------------------------------------
# Stage 5: Schema initialisation (unchanged)
# ---------------------------------------------------------------------------


def test_init_creates_tickets_table(store: TicketStore) -> None:
    with store._lock:
        cur = store._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets';"
        )
        assert cur.fetchone() is not None


def test_init_creates_touch_history_table(store: TicketStore) -> None:
    with store._lock:
        cur = store._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='touch_history';"
        )
        assert cur.fetchone() is not None


def test_init_creates_indexes(store: TicketStore) -> None:
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
    with store._lock:
        cur = store._conn.cursor()
        cur.execute("PRAGMA user_version;")
        version = cur.fetchone()[0]
    assert version == DB_SCHEMA_VERSION


def test_init_is_idempotent() -> None:
    store = TicketStore(":memory:")
    store.init_schema()
    store.init_schema()


def test_store_as_context_manager() -> None:
    with TicketStore(":memory:") as store:
        assert store._conn is not None
    with pytest.raises(Exception):
        store._execute("SELECT 1;")


# ---------------------------------------------------------------------------
# Stage 5: list() filter interface (now returning Ticket objects)
# ---------------------------------------------------------------------------


def _insert_raw(
    store: TicketStore,
    ticket_id: str,
    status: str,
    tier_name: str = "Hamsa",
    assigned_to: str | None = None,
) -> None:
    """Insert a minimal raw ticket row for filter tests."""
    sql = """
        INSERT INTO tickets (
            id, schema_version, tier_name, tier_level, status,
            collaboration_mode, mtap, created_at, updated_at,
            created_by_agent, assigned_to_agent, payload_json,
            failure_notes_json
        ) VALUES (?, '1.0', ?, ?, ?, 'solo', 1,
                  '2024-01-01T00:00:00+00:00',
                  '2024-01-01T00:00:00+00:00',
                  'param-aatma', ?, '{}', '[]');
    """
    with store._lock:
        store._conn.execute(
            sql, (ticket_id, tier_name, HAMSA_LEVEL, status, assigned_to)
        )
        store._conn.commit()


def test_list_returns_ticket_objects(store: TicketStore) -> None:
    """list() must return Ticket objects, not sqlite3.Row objects."""
    _insert_raw(store, "t-001", "queued")
    results = store.list()
    assert len(results) == 1
    assert isinstance(results[0], Ticket)


def test_list_returns_all_when_no_filters(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued")
    _insert_raw(store, "t-002", "done")
    assert len(store.list()) == 2


def test_list_filter_by_single_status(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued")
    _insert_raw(store, "t-002", "done")
    results = store.list(status="queued")
    assert len(results) == 1
    assert results[0].envelope.status == "queued"


def test_list_filter_by_status_in(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued")
    _insert_raw(store, "t-002", "in_progress")
    _insert_raw(store, "t-003", "done")
    results = store.list(status_in={"queued", "in_progress"})
    assert len(results) == 2
    statuses = {t.envelope.status for t in results}
    assert statuses == {"queued", "in_progress"}


def test_list_filter_by_tier(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued", tier_name="Hamsa")
    _insert_raw(store, "t-002", "queued", tier_name="Adi Purusha")
    results = store.list(tier="Hamsa")
    assert len(results) == 1
    assert results[0].envelope.tier_name == "Hamsa"


def test_list_filter_by_assigned_to(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "in_progress", assigned_to="vasishtha")
    _insert_raw(store, "t-002", "in_progress", assigned_to="atri")
    results = store.list(assigned_to="vasishtha")
    assert len(results) == 1
    assert results[0].envelope.assigned_to_agent == "vasishtha"


def test_list_combined_filters(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued",      tier_name="Hamsa")
    _insert_raw(store, "t-002", "in_progress", tier_name="Hamsa")
    _insert_raw(store, "t-003", "queued",      tier_name="Adi Purusha")
    results = store.list(status="queued", tier="Hamsa")
    assert len(results) == 1
    assert results[0].envelope.id == "t-001"


def test_list_status_in_precedence(store: TicketStore) -> None:
    _insert_raw(store, "t-001", "queued")
    _insert_raw(store, "t-002", "done")
    results = store.list(status="done", status_in={"queued"})
    assert len(results) == 1
    assert results[0].envelope.id == "t-001"


def test_list_empty_returns_empty_list(store: TicketStore) -> None:
    assert store.list() == []


# ---------------------------------------------------------------------------
# Stage 6: CRUD
# ---------------------------------------------------------------------------


def test_create_returns_id(store: TicketStore) -> None:
    """create() returns the ticket's id string."""
    ticket = make_ticket()
    returned_id = store.create(ticket)
    assert returned_id == ticket.envelope.id


def test_create_then_read_roundtrip(store: TicketStore) -> None:
    """A ticket written via create() and read back via read() is identical."""
    ticket = make_ticket()
    store.create(ticket)
    restored = store.read(ticket.envelope.id)

    assert restored is not None
    assert restored.envelope.id           == ticket.envelope.id
    assert restored.envelope.tier_name    == ticket.envelope.tier_name
    assert restored.envelope.status       == ticket.envelope.status
    assert restored.envelope.created_by_agent == "param-aatma"
    assert restored.payload               == ticket.payload
    assert restored.result                is None


def test_read_nonexistent_returns_none(store: TicketStore) -> None:
    """read() on an unknown id returns None, not an exception."""
    assert store.read("no-such-id") is None


def test_update_changes_status(store: TicketStore) -> None:
    """update() persists field changes."""
    ticket = make_ticket()
    store.create(ticket)

    ticket.envelope.status = "in_progress"
    ticket.envelope.assigned_to_agent = "vasishtha"
    store.update(ticket)

    restored = store.read(ticket.envelope.id)
    assert restored.envelope.status == "in_progress"
    assert restored.envelope.assigned_to_agent == "vasishtha"


def test_update_stamps_updated_at(store: TicketStore) -> None:
    """update() must advance updated_at beyond created_at."""
    ticket = make_ticket()
    store.create(ticket)
    original_updated = ticket.envelope.updated_at

    ticket.envelope.status = "done"
    store.update(ticket)

    restored = store.read(ticket.envelope.id)
    assert restored.envelope.updated_at >= original_updated


def test_create_duplicate_raises(store: TicketStore) -> None:
    """Inserting two tickets with the same id must raise IntegrityError."""
    ticket = make_ticket()
    store.create(ticket)
    with pytest.raises(sqlite3.IntegrityError):
        store.create(ticket)


def test_on_ticket_write_callback_called(store: TicketStore) -> None:
    """_on_ticket_write callback is invoked after create() and update()."""
    calls = []
    store._on_ticket_write = lambda t: calls.append(t.envelope.id)

    ticket = make_ticket()
    store.create(ticket)
    ticket.envelope.status = "done"
    store.update(ticket)

    assert len(calls) == 2
    assert all(c == ticket.envelope.id for c in calls)


# ---------------------------------------------------------------------------
# Stage 6: append_failure_note
# ---------------------------------------------------------------------------


def test_append_failure_note_adds_to_list(store: TicketStore) -> None:
    """append_failure_note() adds a note and preserves prior entries."""
    ticket = make_ticket()
    store.create(ticket)

    note1 = FailureNote(
        ticket_id=ticket.envelope.id,
        agent="vasishtha",
        failed_at=datetime.now(timezone.utc),
        reason="bad output",
    )
    note2 = FailureNote(
        ticket_id=ticket.envelope.id,
        agent="atri",
        failed_at=datetime.now(timezone.utc),
        reason="timeout",
    )

    store.append_failure_note(ticket.envelope.id, note1)
    store.append_failure_note(ticket.envelope.id, note2)

    restored = store.read(ticket.envelope.id)
    assert len(restored.envelope.failure_notes) == 2
    assert restored.envelope.failure_notes[0].reason == "bad output"
    assert restored.envelope.failure_notes[1].reason == "timeout"


def test_append_failure_note_nonexistent_raises(store: TicketStore) -> None:
    """append_failure_note() on an unknown ticket_id raises ValueError."""
    note = FailureNote(
        ticket_id="ghost",
        agent="vasishtha",
        failed_at=datetime.now(timezone.utc),
        reason="never happened",
    )
    with pytest.raises(ValueError, match="not found"):
        store.append_failure_note("ghost", note)


def test_append_failure_note_preserves_existing(store: TicketStore) -> None:
    """
    append_failure_note() must not overwrite existing notes —
    the list is append-only.
    """
    ticket = make_ticket()
    store.create(ticket)

    for i in range(3):
        store.append_failure_note(
            ticket.envelope.id,
            FailureNote(
                ticket_id=ticket.envelope.id,
                agent=f"agent-{i}",
                failed_at=datetime.now(timezone.utc),
                reason=f"reason-{i}",
            ),
        )

    restored = store.read(ticket.envelope.id)
    assert len(restored.envelope.failure_notes) == 3
    assert [fn.reason for fn in restored.envelope.failure_notes] == [
        "reason-0", "reason-1", "reason-2"
    ]


# ---------------------------------------------------------------------------
# Stage 6: append_touch
# ---------------------------------------------------------------------------


def test_append_touch_stored_and_read_back(store: TicketStore) -> None:
    """append_touch() inserts into touch_history; read() attaches it."""
    ticket = make_ticket()
    store.create(ticket)

    touch = TouchEntry(
        agent="vasishtha",
        started=datetime.now(timezone.utc),
    )
    store.append_touch(touch, ticket.envelope.id)

    restored = store.read(ticket.envelope.id)
    assert len(restored.envelope.touch_history) == 1
    assert restored.envelope.touch_history[0].agent == "vasishtha"
    assert restored.envelope.touch_history[0].ended is None


def test_append_touch_multiple_entries_ordered(store: TicketStore) -> None:
    """Touch history entries are returned in started ASC order."""
    ticket = make_ticket()
    store.create(ticket)

    for name in ["vasishtha", "atri", "agastya"]:
        store.append_touch(
            TouchEntry(agent=name, started=datetime.now(timezone.utc)),
            ticket.envelope.id,
        )

    restored = store.read(ticket.envelope.id)
    agents = [t.agent for t in restored.envelope.touch_history]
    assert agents == ["vasishtha", "atri", "agastya"]


# ---------------------------------------------------------------------------
# Stage 6: migrate-on-read
# ---------------------------------------------------------------------------


def test_migrate_on_read_upgrades_old_ticket(store: TicketStore) -> None:
    """
    A ticket stored with schema_version='0.9' must be migrated to '1.0'
    on read. The stub migration is a no-op except for the version stamp,
    so all other fields remain intact.
    """
    # Insert a raw 0.9 ticket directly — bypassing create() which would
    # use the current schema version
    with store._lock:
        store._conn.execute(
            """INSERT INTO tickets (
                id, schema_version, tier_name, tier_level, status,
                collaboration_mode, mtap, created_at, updated_at,
                created_by_agent, payload_json, failure_notes_json
            ) VALUES ('t-old', '0.9', 'Hamsa', ?, 'queued', 'solo', 1,
                      '2024-01-01T00:00:00+00:00',
                      '2024-01-01T00:00:00+00:00',
                      'param-aatma', '{}', '[]')""",
            (HAMSA_LEVEL,)
        )

    restored = store.read("t-old")
    assert restored is not None
    assert restored.envelope.schema_version == "1.0"