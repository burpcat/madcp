# madhu/store/sqlite.py
"""
SQLite ticket store for MadCP — madhu.

TicketStore is the single point of access to the SQLite database.
No other module opens the database file directly.

Two tables:
  tickets       — one row per ticket; structured sub-objects stored as JSON blobs
  touch_history — one row per touch event; queryable independently of tickets

Threading model (v0):
  Single connection, protected by a threading.Lock. Safe for a single-process
  server with a background scheduler thread. Each multiprocessing worker
  (stage 11) opens its own short-lived connection rather than sharing this one.

Schema versioning:
  The tickets table records schema_version per ticket. The migrate-on-read
  system in madhu/schemas/migrations/ handles upgrades at deserialisation time.
  The SQLite schema itself (table structure) is versioned via a user_version
  PRAGMA — incremented manually if the table structure changes.

Markdown sync:
  After every create/update, if _on_ticket_write is set, it is called with
  the Ticket object. Set this after construction:
      store._on_ticket_write = markdown_sync.sync_ticket
  This avoids a hard import dependency on stage 7 from stage 6.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from madhu.schemas.envelope import Envelope, FailureNote, Ticket, TouchEntry
from madhu.schemas.migrations import migrate

# Current SQLite schema version (table structure, not ticket schema).
# Increment this if a column is added or removed.
DB_SCHEMA_VERSION = 1

# Statuses that count as "active" for capacity and naming checks.
ACTIVE_STATUSES = {"queued", "touched", "in_progress"}


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TICKETS = """
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

_CREATE_TOUCH_HISTORY = """
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

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tickets_status   ON tickets(status);",
    "CREATE INDEX IF NOT EXISTS idx_tickets_tier     ON tickets(tier_name);",
    "CREATE INDEX IF NOT EXISTS idx_tickets_assigned ON tickets(assigned_to_agent);",
]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: datetime | None) -> str | None:
    """Serialise a datetime to ISO-8601 string for SQLite storage."""
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    """Deserialise an ISO-8601 string from SQLite into a timezone-aware datetime."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Assume UTC for any naive datetime stored before timezone enforcement
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ticket_to_row(ticket: Ticket) -> dict:
    """
    Flatten a Ticket object into a dict of scalar values suitable for
    INSERT/UPDATE. Structured sub-objects (payload, result, failure_notes)
    are serialised to JSON strings.
    """
    env = ticket.envelope
    return {
        "id":                 env.id,
        "parent_id":          env.parent_id,
        "forwarded_from":     env.forwarded_from,
        "schema_version":     env.schema_version,
        "tier_name":          env.tier_name,
        "tier_level":         env.tier_level,
        "status":             env.status,
        "collaboration_mode": env.collaboration_mode,
        "mtap":               int(env.mtap),
        "created_at":         _dt_to_str(env.created_at),
        "updated_at":         _dt_to_str(env.updated_at),
        "created_by_agent":   env.created_by_agent,
        "assigned_to_agent":  env.assigned_to_agent,
        "touched_by":         env.touched_by,
        "payload_json":       json.dumps(ticket.payload),
        "result_json":        ticket.result.model_dump_json()
                              if ticket.result else None,
        "failure_notes_json": json.dumps([
            fn.model_dump(mode="json") for fn in env.failure_notes
        ]),
    }


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    """
    Reconstruct a Ticket from a sqlite3.Row.

    Runs migrate() on the raw dict before deserialisation — this is the
    migrate-on-read contract. migrate() is a fast no-op for current-version
    tickets, so the overhead is negligible.

    touch_history is not stored in the tickets table — it lives in the
    touch_history table. This function reconstructs the envelope without
    touch history; callers that need full touch history must join separately
    or call read() which handles it.
    """
    # Build a nested dict matching the Ticket model shape
    ticket_dict = {
        "envelope": {
            "id":                 row["id"],
            "parent_id":          row["parent_id"],
            "forwarded_from":     row["forwarded_from"],
            "schema_version":     row["schema_version"],
            "tier_name":          row["tier_name"],
            "tier_level":         row["tier_level"],
            "status":             row["status"],
            "collaboration_mode": row["collaboration_mode"],
            "mtap":               bool(row["mtap"]),
            "created_at":         row["created_at"],
            "updated_at":         row["updated_at"],
            "created_by_agent":   row["created_by_agent"],
            "assigned_to_agent":  row["assigned_to_agent"],
            "touched_by":         row["touched_by"],
            "touch_history": [],  # not populated by list() — call read() for full audit trail
            "failure_notes":      json.loads(row["failure_notes_json"] or "[]"),
        },
        "payload": json.loads(row["payload_json"]),
        "result":  json.loads(row["result_json"]) if row["result_json"] else None,
    }

    # Migrate-on-read: bring any old-versioned ticket up to current schema
    ticket_dict = migrate(ticket_dict)

    return Ticket.model_validate(ticket_dict)


# ---------------------------------------------------------------------------
# TicketStore
# ---------------------------------------------------------------------------


class TicketStore:
    """
    The single point of access to the MadCP SQLite database.

    Opens the database file on construction and initialises the schema
    if the tables don't exist yet. Safe to construct multiple times
    against the same file — all DDL uses IF NOT EXISTS.

    Args:
        db_path: Path to the SQLite file. Will be created if it doesn't
                 exist. Pass ":memory:" for an in-memory database in tests.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        # check_same_thread=False because the scheduler thread and the
        # MCP handler thread both access the store. The threading.Lock
        # below serialises all access — SQLite itself is not thread-safe
        # without this guard.
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        # Row factory makes column access by name possible:
        # row["status"] instead of row[6].
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

        # Optional callback — set after construction to wire in markdown sync.
        # Signature: (ticket: Ticket) -> None
        # Called after the lock is released — ticket state reflects the moment
        # of write, not necessarily current SQLite state. Acceptable for v0
        # markdown sync. Calling inside the lock risks blocking on slow I/O.
        self._on_ticket_write: Callable[[Ticket], None] | None = None

        self.init_schema()

    def init_schema(self) -> None:
        """
        Create tables and indexes if they don't exist, and stamp the
        DB_SCHEMA_VERSION into the user_version PRAGMA.

        Safe to call multiple times — all DDL is IF NOT EXISTS.

        Raises RuntimeError if the database's existing schema version is
        newer than the current codebase version — this prevents an older
        codebase from silently overwriting a newer database's version signal.
        """
        with self._lock:
            cur = self._conn.cursor()

            # Guard against opening a newer database with older code.
            cur.execute("PRAGMA user_version;")
            existing_version = cur.fetchone()[0]
            if existing_version > DB_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {existing_version} is newer than "
                    f"codebase version {DB_SCHEMA_VERSION}. "
                    f"Upgrade the codebase before opening this database."
                )

            cur.execute(_CREATE_TICKETS)
            cur.execute(_CREATE_TOUCH_HISTORY)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
            cur.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION};")
            self._conn.commit()

    # Use _execute() for simple fixed-SQL statements.
    # For dynamic queries (variable WHERE clauses, IN lists),
    # acquire self._lock directly as list() does.
    def _execute(self,
        sql: str,
        params: tuple | dict = (),
        *,
        fetch: str | None = None,) -> list[sqlite3.Row] | sqlite3.Row | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            self._conn.commit()
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            return None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, ticket: Ticket) -> str:
        """
        Insert a new ticket into the store. Returns the ticket id.

        Raises sqlite3.IntegrityError if a ticket with the same id
        already exists.

        Calls _on_ticket_write after a successful insert if set.
        """
        row = _ticket_to_row(ticket)
        sql = """
            INSERT INTO tickets (
                id, parent_id, forwarded_from, schema_version,
                tier_name, tier_level, status, collaboration_mode, mtap,
                created_at, updated_at, created_by_agent,
                assigned_to_agent, touched_by,
                payload_json, result_json, failure_notes_json
            ) VALUES (
                :id, :parent_id, :forwarded_from, :schema_version,
                :tier_name, :tier_level, :status, :collaboration_mode, :mtap,
                :created_at, :updated_at, :created_by_agent,
                :assigned_to_agent, :touched_by,
                :payload_json, :result_json, :failure_notes_json
            );
        """
        self._execute(sql, row)
        if self._on_ticket_write:
            # self._on_ticket_write(ticket)
            self._on_ticket_write(self.read(ticket.envelope.id))
        return ticket.envelope.id

    def read(self, ticket_id: str) -> Ticket | None:
        """
        Retrieve a single ticket by id. Returns None if not found.

        Fetches touch history from the touch_history table and attaches
        it to the envelope before returning — callers get the full
        ticket including audit trail.

        Runs migrate-on-read before deserialising.
        """
        row = self._execute(
            "SELECT * FROM tickets WHERE id = ?;",
            (ticket_id,),
            fetch="one",
        )
        if row is None:
            return None

        ticket = _row_to_ticket(row)

        # Attach touch history from the separate table
        touch_rows = self._execute(
            "SELECT * FROM touch_history WHERE ticket_id = ? ORDER BY started ASC;",
            (ticket_id,),
            fetch="all",
        )
        ticket.envelope.touch_history = [
            TouchEntry(
                agent=r["agent"],
                # started=_str_to_dt(r["started"]),
                started=r["started"],
                # ended=_str_to_dt(r["ended"]),
                ended=r["ended"],
                summary=r["summary"],
            )
            for r in (touch_rows or [])
        ]

        return ticket

    def update(self, ticket: Ticket) -> None:
        """
        Overwrite an existing ticket by id.

        NOTE: mutates ticket.envelope.updated_at in place — the caller's
        object will reflect the new timestamp after this call returns.
        The touch manager (stage 8) should account for this when holding
        ticket state across calls.
        ...
        """
        # Stamp updated_at — mutates the passed-in object intentionally.
        # See docstring note above.
        ticket.envelope.updated_at = datetime.now(timezone.utc)

        row = _ticket_to_row(ticket)
        sql = """
            UPDATE tickets SET
                parent_id           = :parent_id,
                forwarded_from      = :forwarded_from,
                schema_version      = :schema_version,
                tier_name           = :tier_name,
                tier_level          = :tier_level,
                status              = :status,
                collaboration_mode  = :collaboration_mode,
                mtap                = :mtap,
                updated_at          = :updated_at,
                assigned_to_agent   = :assigned_to_agent,
                touched_by          = :touched_by,
                payload_json        = :payload_json,
                result_json         = :result_json,
                failure_notes_json  = :failure_notes_json
            WHERE id = :id;
        """
        self._execute(sql, row)
        if self._on_ticket_write:
            # self._on_ticket_write(ticket)
            self._on_ticket_write(self.read(ticket.envelope.id))

    def list(
        self,
        status: str | None = None,
        status_in: set[str] | None = None,
        tier: str | None = None,
        assigned_to: str | None = None,
    ) -> list[Ticket]:
        """
        Query tickets with optional filters. Returns fully deserialised
        Ticket objects with migrate-on-read applied.

        Note: touch_history is NOT populated on list() results for
        performance — only read() fetches the full touch trail.
        If you need touch history, call read(ticket.envelope.id).

        Args:
            status:      Filter to a single status value.
            status_in:   Filter to any of a set of status values.
                         Takes precedence over status if both provided.
                         Required by the naming service (stage 4).
            tier:        Filter by tier_name.
            assigned_to: Filter by assigned_to_agent.
        """
        conditions: list[str] = []
        params: list = []

        if status_in is not None:
            placeholders = ",".join("?" * len(status_in))
            conditions.append(f"status IN ({placeholders})")
            # sort for deterministic query plans and reproducible test behaviour
            params.extend(sorted(status_in))
        elif status is not None:
            conditions.append("status = ?")
            params.append(status)

        if tier is not None:
            conditions.append("tier_name = ?")
            params.append(tier)

        if assigned_to is not None:
            conditions.append("assigned_to_agent = ?")
            params.append(assigned_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM tickets {where} ORDER BY created_at ASC;"

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [_row_to_ticket(r) for r in rows]

    def append_failure_note(self, ticket_id: str, note: FailureNote) -> None:
        """
        Append a FailureNote to a ticket's failure_notes list.

        Reads the existing JSON blob, appends the new note, and writes
        back atomically under the lock. Does not deserialise the full
        ticket — only the failure_notes array is touched.

        Raises ValueError if the ticket does not exist.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT failure_notes_json FROM tickets WHERE id = ?;",
                (ticket_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"Cannot append failure note: ticket {ticket_id!r} not found."
                )

            notes = json.loads(row["failure_notes_json"] or "[]")
            notes.append(note.model_dump(mode="json"))

            cur.execute(
                "UPDATE tickets SET failure_notes_json = ?, updated_at = ? WHERE id = ?;",
                (json.dumps(notes), _dt_to_str(datetime.now(timezone.utc)), ticket_id),
            )
            self._conn.commit()
            # Trigger markdown sync if wired — fetch fresh state after commit
            if self._on_ticket_write:
                refreshed = self.read(ticket_id)
                if refreshed:
                    self._on_ticket_write(refreshed)
            
            # if self._on_ticket_write:
            #     full = self.read(ticket_id)
            #     if full is not None:
            #         self._on_ticket_write(full)
    
    def _acquire_touch(self, ticket_id: str, agent_name: str) -> bool:
        """
        Atomically acquire a ticket for an agent using BEGIN IMMEDIATE.

        Returns True if acquisition succeeded (ticket was queued).
        Returns False if ticket does not exist or is not in queued status.

        BEGIN IMMEDIATE takes a write lock on the database for the duration
        of the transaction, serialising concurrent acquire attempts across
        threads and processes.
        """
        now = _dt_to_str(datetime.now(timezone.utc))
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "SELECT status FROM tickets WHERE id = ?",
                    (ticket_id,),
                )
                row = cur.fetchone()
                if row is None or row["status"] != "queued":
                    self._conn.rollback()
                    return False

                cur.execute(
                    """
                    UPDATE tickets
                    SET status = 'touched',
                        assigned_to_agent = ?,
                        touched_by = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (agent_name, agent_name, now, ticket_id),
                )
                self._conn.commit()
                # append_touch() is called after commit intentionally.
                # Pulling it inside the transaction would deadlock since _execute() also locks.
                # The narrow window where status=touched but touch_history is empty is benign —
                # the scheduler dispatches on status, not touch_history.
            except Exception:
                self._conn.rollback()
                raise

        # touch = TouchEntry(
        #     agent=agent_name,
        #     started=now,
        #     ended=now,   # will be overwritten by _close_touch_entry on release
        #     summary="",  # will be overwritten on release
        # )
        # Append open touch entry (ended=None marks it as open)
        touch = TouchEntry(
        agent=agent_name,
        started=now,
        ended=None,  # open touch; set by _close_touch_entry on release
        summary="",
    )
        self.append_touch(touch, ticket_id)

        # Sync markdown
        if self._on_ticket_write:
            full = self.read(ticket_id)
            if full is not None:
                self._on_ticket_write(full)

        return True

    def _close_touch_entry(
        self,
        ticket_id: str,
        agent_name: str,
        ended: str,
        summary: str,
    ) -> None:
        """
        Close the most recent open touch entry for (ticket_id, agent_name).

        Updates the touch_history row with ended and summary. Called by
        TouchManager.release() and TouchManager.forward().
        """
        self._execute(
            """
            UPDATE touch_history
            SET ended = ?, summary = ?
            WHERE ticket_id = ? AND agent = ?
            AND id = (
                SELECT id FROM touch_history
                WHERE ticket_id = ? AND agent = ?
                ORDER BY started DESC
                LIMIT 1
            )
            """,
            (ended, summary, ticket_id, agent_name, ticket_id, agent_name),
        )

    def append_touch(self, touch: TouchEntry, ticket_id: str) -> None:
        """
        Insert a TouchEntry into the touch_history table for a ticket.

        The touch_history table is append-only — entries are never updated
        or deleted. The touch manager (stage 8) updates the 'ended' and
        'summary' fields of an open touch entry by ticket_id + agent + started.
        """
        self._execute(
            """
            INSERT INTO touch_history (ticket_id, agent, started, ended, summary)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                ticket_id,
                touch.agent,
                _dt_to_str(touch.started),
                _dt_to_str(touch.ended),
                touch.summary,
            ),
        )

        if self._on_ticket_write:
            full = self.read(touch.ticket_id)  # or however your stage 6 identifies the ticket
            if full is not None:
                self._on_ticket_write(full)

    def close(self) -> None:
        """Close the database connection. Safe to call multiple times."""
        with self._lock:
            self._conn.close()

    @property
    def db_path(self) -> str:
        return self._db_path

    def __enter__(self) -> "TicketStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()