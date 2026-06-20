from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TierRow:
    name: str
    active: int
    queued: int
    in_progress: int


@dataclass(frozen=True)
class AgentRow:
    agent_name: str
    tier: str
    ticket_id: str
    elapsed_seconds: float


@dataclass(frozen=True)
class TicketRow:
    ticket_id: str
    tier: str
    status: str
    agent: str
    elapsed_seconds: float | None
    forwarded_to: str | None


@dataclass(frozen=True)
class DashboardSnapshot:
    waiting: bool = False
    tiers: list[TierRow] = field(default_factory=list)
    agents: list[AgentRow] = field(default_factory=list)
    recent_tickets: list[TicketRow] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)


def fetch_snapshot(
    db_path: str,
    log_path: str = "logs/runs.jsonl",
) -> DashboardSnapshot:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return DashboardSnapshot(waiting=True)

    try:
        tier_rows = _fetch_tier_rows(con)
        agent_rows = _fetch_agent_rows(con)
        recent_tickets = _fetch_recent_tickets(con)
        log_tail = _read_log_tail(log_path, n=20)
        return DashboardSnapshot(
            waiting=False,
            tiers=tier_rows,
            agents=agent_rows,
            recent_tickets=recent_tickets,
            log_tail=log_tail,
        )
    except sqlite3.Error:
        return DashboardSnapshot(waiting=True)
    finally:
        con.close()


def _fetch_tier_rows(con: sqlite3.Connection) -> list[TierRow]:
    rows = con.execute(
        """
        SELECT
            tier_name,
            tier_level,
            COUNT(CASE WHEN status IN ('touched', 'in_progress') THEN 1 END) AS active,
            COUNT(CASE WHEN status = 'queued'       THEN 1 END) AS queued,
            COUNT(CASE WHEN status = 'in_progress'  THEN 1 END) AS in_progress
        FROM tickets
        GROUP BY tier_name, tier_level
        ORDER BY tier_level ASC
        """
    ).fetchall()
    return [
        TierRow(
            name=r["tier_name"],
            active=r["active"],
            queued=r["queued"],
            in_progress=r["in_progress"],
        )
        for r in rows
    ]


def _fetch_agent_rows(con: sqlite3.Connection) -> list[AgentRow]:
    # julianday('now') is UTC; stored timestamps are UTC ISO-8601 — subtraction is correct.
    rows = con.execute(
        """
        SELECT
            assigned_to_agent,
            tier_name,
            id,
            (julianday('now') - julianday(updated_at)) * 86400.0 AS elapsed
        FROM tickets
        WHERE status IN ('touched', 'in_progress')
          AND assigned_to_agent IS NOT NULL
        ORDER BY updated_at ASC
        """
    ).fetchall()
    return [
        AgentRow(
            agent_name=r["assigned_to_agent"],
            tier=r["tier_name"],
            ticket_id=r["id"][:6],
            elapsed_seconds=r["elapsed"] or 0.0,
        )
        for r in rows
    ]


def _fetch_recent_tickets(con: sqlite3.Connection) -> list[TicketRow]:
    # Single LEFT JOIN — no N+1.
    # t2.id is the ticket that superseded t (i.e. was forwarded_from=t.id).
    rows = con.execute(
        """
        SELECT
            t.id,
            t.tier_name,
            t.status,
            t.assigned_to_agent,
            (julianday('now') - julianday(t.created_at)) * 86400.0 AS elapsed,
            t2.id AS forwarded_to_id
        FROM tickets t
        LEFT JOIN tickets t2 ON t2.forwarded_from = t.id
        ORDER BY t.updated_at DESC
        LIMIT 5
        """
    ).fetchall()
    return [
        TicketRow(
            ticket_id=r["id"][:6],
            tier=r["tier_name"],
            status=r["status"],
            agent=r["assigned_to_agent"] or "",
            elapsed_seconds=r["elapsed"],
            forwarded_to=r["forwarded_to_id"][:6] if r["forwarded_to_id"] else None,
        )
        for r in rows
    ]


def _read_log_tail(log_path: str, n: int = 20) -> list[str]:
    path = Path(log_path)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-n:] if len(lines) > n else lines
    except OSError:
        return []