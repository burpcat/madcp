# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/store/markdown.py
from __future__ import annotations

"""
Markdown sync for MadCP ticket store.

Keeps tickets/ in sync with SQLite. One .md file per ticket, named {id}.md.
Every ticket write (create or update) triggers sync_ticket(). Deletes remove the file.

Sync is one-way: SQLite → markdown. Never read back.

Called by:
- TicketStore._on_ticket_write (wired during server init at stage 12)
- sync_all() for bulk reconciliation (e.g. on startup)

Source object: always pass a full Ticket from store.read(), not store.list().
store.list() omits touch_history by design (N+1 avoidance). sync_ticket()
receiving a list()-sourced ticket will produce a file with an empty touch history
section — silent and wrong. The wiring via _on_ticket_write uses store.read()
internally and is safe.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from madhu.schemas.envelope import Ticket

if TYPE_CHECKING:
    from madhu.store.sqlite import TicketStore


def _wiki(ticket_id: str | None) -> str | None:
    """Return Obsidian wiki-link for a ticket id, or None if id is None."""
    if ticket_id is None:
        return None
    return f"[[{ticket_id}]]"


def _build_frontmatter(ticket: Ticket) -> dict:
    """
    Build the YAML frontmatter dict from a ticket's envelope.

    parent_id and forwarded_from are rendered as wiki-links so Obsidian
    builds graph edges between ticket files automatically.
    """
    env = ticket.envelope
    fm: dict = {
        "id": env.id,
        "schema_version": env.schema_version,
        "tier_name": env.tier_name,
        "tier_level": env.tier_level,
        "status": env.status,  # plain string due to use_enum_values=True
        "collaboration_mode": env.collaboration_mode,
        "mtap": env.mtap,
        "created_at": env.created_at,
        "updated_at": env.updated_at,
        "created_by_agent": env.created_by_agent,
        "assigned_to_agent": env.assigned_to_agent,
        "touched_by": env.touched_by,
    }

    # Wiki-links for graph edges in Obsidian
    parent_link = _wiki(env.parent_id)
    forwarded_link = _wiki(env.forwarded_from)
    if parent_link is not None:
        fm["parent_id"] = parent_link
    else:
        fm["parent_id"] = None
    if forwarded_link is not None:
        fm["forwarded_from"] = forwarded_link
    else:
        fm["forwarded_from"] = None

    return fm


def _build_body(ticket: Ticket) -> str:
    """
    Build the markdown body (everything after the frontmatter block).

    Sections:
    - ## Payload
    - ## Touch History
    - ## Failure Notes
    - ## Result
    """
    lines: list[str] = []

    # --- Payload ---
    lines.append("## Payload\n")
    payload = ticket.payload
    if payload is not None:
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
        lines.append("```yaml")
        lines.append(yaml.dump(payload_dict, default_flow_style=False).rstrip())
        lines.append("```")
    else:
        lines.append("*(no payload)*")
    lines.append("")

    # --- Touch History ---
    lines.append("## Touch History\n")
    if ticket.envelope.touch_history:
        for entry in ticket.envelope.touch_history:
            agent_display = entry.agent
            # Future: lineage path will be on the agent; for now just the name
            lines.append(f"- **{agent_display}**")
            lines.append(f"  - started: `{entry.started}`")
            lines.append(f"  - ended: `{entry.ended}`")
            lines.append(f"  - summary: {entry.summary}")
        lines.append("")
    else:
        lines.append("*(none yet)*\n")

    # --- Failure Notes ---
    lines.append("## Failure Notes\n")
    if ticket.envelope.failure_notes:
        for i, note in enumerate(ticket.envelope.failure_notes, start=1):
            lines.append(f"### Failure {i}\n")
            lines.append(f"- **ticket_id:** [[{note.ticket_id}]]")
            lines.append(f"- **agent:** {note.agent}")
            lines.append(f"- **failed_at:** `{note.failed_at}`")
            lines.append(f"- **reason:** {note.reason}")
            if note.raw_excerpt:
                lines.append(f"- **raw_excerpt:**")
                lines.append(f"  ```")
                lines.append(f"  {note.raw_excerpt}")
                lines.append(f"  ```")
            lines.append("")
    else:
        lines.append("*(none)*\n")

    # --- Result ---
    lines.append("## Result\n")
    result = ticket.result
    if result is not None:
        lines.append(f"- **status:** {result.status}")
        lines.append(f"- **by_agent:** {result.by_agent}")
        lines.append(f"- **produced_at:** `{result.produced_at}`")
        lines.append(f"- **data:**")
        lines.append(f"  ```")
        # Truncate very long data to avoid huge files; full data is in SQLite
        data_str = str(result.data)
        if len(data_str) > 2000:
            data_str = data_str[:2000] + "\n... (truncated; see SQLite for full data)"
        lines.append(f"  {data_str}")
        lines.append(f"  ```")
    else:
        lines.append("*(pending)*\n")

    return "\n".join(lines)


def _render_ticket(ticket: Ticket) -> str:
    """
    Render a full ticket as a markdown string.

    Format:
        ---
        <YAML frontmatter>
        ---

        # <ticket id>

        <body sections>
    """
    fm_dict = _build_frontmatter(ticket)
    # yaml.dump produces a trailing newline; we strip and re-add for control
    fm_str = yaml.dump(fm_dict, default_flow_style=False, allow_unicode=True).rstrip()
    body = _build_body(ticket)

    return f"---\n{fm_str}\n---\n\n# {ticket.envelope.id}\n\n{body}"


class MarkdownSync:
    """
    Keeps the tickets/ directory in sync with SQLite.

    One file per ticket: tickets/{id}.md. Written on every create/update,
    deleted when delete_ticket() is called.

    Thread safety: Path.write_text() is atomic at the OS level for files of
    this size on any POSIX filesystem. No additional locking needed here —
    the store's threading.Lock ensures only one write happens at a time anyway.

    Obsidian compatibility:
    - YAML frontmatter delimited by ---
    - parent_id and forwarded_from rendered as [[wiki-links]]
    - failure_notes ticket references rendered as [[wiki-links]]
    - Section headers use ## for Obsidian outline view
    """

    def __init__(self, tickets_dir: Path | str) -> None:
        """
        Initialise MarkdownSync pointed at a directory.

        The directory is created if it does not exist. tickets_dir should be
        the project-root tickets/ directory (stage 1 creates it).
        """
        self.tickets_dir = Path(tickets_dir)
        self.tickets_dir.mkdir(parents=True, exist_ok=True)

    def sync_ticket(self, ticket: Ticket) -> None:
        """
        Write (or overwrite) the markdown file for a ticket.

        Called by TicketStore._on_ticket_write after every create/update.
        Receives a full Ticket including touch_history (store.read() sourced).

        If the write fails (e.g. disk full), the exception propagates. The
        caller (TicketStore) does not catch it — a sync failure is surfaced,
        not silenced.
        """
        path = self.tickets_dir / f"{ticket.envelope.id}.md"
        content = _render_ticket(ticket)
        path.write_text(content, encoding="utf-8")

    def sync_all(self, store: TicketStore) -> None:
        """
        Sync all tickets from the store to tickets/.

        Uses store.read() per ticket (not list()) to ensure touch_history is
        populated. Intended for startup reconciliation or manual repair.

        Does not delete orphan .md files (tickets that exist in tickets/ but
        not in SQLite). Orphan cleanup is a manual operation.
        """
        tickets = store.list()
        for stub in tickets:
            # list() returns tickets without touch_history; re-read for full object
            full = store.read(stub.envelope.id)
            if full is not None:
                self.sync_ticket(full)

    def delete_ticket(self, ticket_id: str) -> None:
        """
        Delete the markdown file for a ticket.

        Silent no-op if the file does not exist (idempotent). Called when
        a ticket is hard-deleted from SQLite (rare — mostly for test cleanup).
        """
        path = self.tickets_dir / f"{ticket_id}.md"
        path.unlink(missing_ok=True)
