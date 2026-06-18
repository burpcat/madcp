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
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

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

    def _execute(
        self,
        sql: str,
        params: tuple = (),
        *,
        fetch: str | None = None,
    ) -> list[sqlite3.Row] | sqlite3.Row | None:
        """
        Internal helper: acquire the lock, run a statement, commit,
        and return results.

        Args:
            sql:    The SQL to execute.
            params: Bind parameters (use ? placeholders in sql).
            fetch:  "all" → fetchall(), "one" → fetchone(), None → no fetch.

        Returns:
            List of rows, single row, or None depending on fetch mode.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            self._conn.commit()
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            return None

    def list(
        self,
        status: str | None = None,
        status_in: set[str] | None = None,
        tier: str | None = None,
        assigned_to: str | None = None,
    ) -> list:
        """
        Query tickets with optional filters. Returns raw sqlite3.Row
        objects at this stage — stage 6 will add deserialisation.

        Args:
            status:     Filter to a single status value.
            status_in:  Filter to any of a set of status values.
                        Takes precedence over status if both provided.
                        Required by the naming service (stage 4).
            tier:       Filter by tier_name.
            assigned_to: Filter by assigned_to_agent.

        Returns:
            List of sqlite3.Row objects (stage 6 converts these to Ticket).
        """
        conditions: list[str] = []
        params: list = []

        if status_in is not None:
            # Build an IN clause with one ? placeholder per status value.
            placeholders = ",".join("?" * len(status_in))
            conditions.append(f"status IN ({placeholders})")
            params.extend(sorted(status_in))  # sorted for deterministic queries
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
            return cur.fetchall()

    def close(self) -> None:
        """
        Close the database connection. Call this on server shutdown.
        Safe to call multiple times.
        """
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Context manager support — allows: with TicketStore(...) as store:
    # ------------------------------------------------------------------

    def __enter__(self) -> "TicketStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
