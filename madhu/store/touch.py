# madhu/store/touch.py
from __future__ import annotations

"""
Touch protocol for MadCP.

Enforces the invariant: a ticket is worked by exactly one agent at a time.

Three operations:
- acquire(ticket_id, agent_name) -> bool
  Atomically claim a queued ticket. Returns False if already claimed.
- release(ticket_id, agent_name, summary, status_after)
  Close the open touch entry and transition ticket status.
- forward(ticket_id, reason, raw_excerpt)
  Kill the current ticket, create a new one with appended failure_notes,
  linked via forwarded_from. A different agent will pick up the new ticket.

All mutations go through TicketStore — touch.py never writes to SQLite directly.
The atomicity guarantee on acquire() comes from BEGIN IMMEDIATE on the store's
connection, accessed via TicketStore._acquire_touch().

Called by:
- Gemma worker (stage 9): acquire → work → release or forward
- Scheduler (stage 11): monitors touch state via store.list()
- Failure forwarding (stage 14): forward() drives the retry chain
"""

import uuid
from datetime import datetime, timezone

from madhu.schemas.envelope import (
    Envelope,
    FailureNote,
    Ticket,
    TicketStatus,
    TouchEntry,
)
from madhu.store.sqlite import TicketStore


def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class TouchManager:
    """
    Coordinates ticket ownership across agents.

    Uses TicketStore as the sole persistence layer. The atomicity of
    acquire() depends on TicketStore._acquire_touch() using BEGIN IMMEDIATE,
    which serialises concurrent acquire attempts at the SQLite level.

    One TouchManager instance is shared across all workers within a process.
    Workers in separate processes (MTap, stage 9) each instantiate their own
    TouchManager backed by the same SQLite file — SQLite's write locking
    ensures correctness across processes.
    """

    def __init__(self, store: TicketStore) -> None:
        """
        Initialise with a TicketStore instance.

        The store must already be initialised (init_schema() called).
        """
        self.store = store

    def acquire(self, ticket_id: str, agent_name: str, logger=None) -> bool:
        """
        Atomically claim a ticket for an agent.

        Returns True if the claim succeeded (ticket was queued and is now
        touched). Returns False if the ticket is not in a claimable state
        (already touched, in_progress, done, failed, killed, forwarded,
        aborted) or does not exist.

        On success:
        - ticket.status → 'touched'
        - ticket.assigned_to_agent → agent_name
        - ticket.touched_by → agent_name
        - A TouchEntry with ended=None is appended to touch_history

        Atomicity: the read-check-write is wrapped in BEGIN IMMEDIATE inside
        TicketStore._acquire_touch(). Two concurrent callers will serialise;
        exactly one will see status='queued' and succeed.
        """
        return self.store._acquire_touch(ticket_id, agent_name)

    def release(
        self,
        ticket_id: str,
        agent_name: str,
        summary: str,
        status_after: str,
    ) -> None:
        """
        Close the open touch entry and set the ticket's terminal status.

        status_after must be one of: 'done', 'failed', 'killed'.
        (Forwarded and aborted are set by forward(), not release().)

        On success:
        - The open TouchEntry (ended=None) for this agent is closed with
          ended=now and the provided summary.
        - ticket.status → status_after
        - ticket.touched_by → None
        - ticket.updated_at → now

        Raises ValueError if:
        - ticket does not exist
        - ticket is not currently touched/in_progress by agent_name
        - status_after is not a valid terminal status for release()
        """
        valid_statuses = {"done", "failed", "killed"}
        if status_after not in valid_statuses:
            raise ValueError(
                f"release() status_after {status_after!r} must be one of {valid_statuses}"
            )

        ticket = self.store.read(ticket_id)
        if ticket is None:
            raise ValueError(f"release(): ticket {ticket_id!r} does not exist")

        if ticket.envelope.touched_by != agent_name:
            raise ValueError(
                f"release(): ticket {ticket_id!r} is not currently held by {agent_name!r} "
                f"(held by {ticket.envelope.touched_by!r})"
            )

        ended = _now()

        # Close the open touch entry for this agent
        self.store._close_touch_entry(ticket_id, agent_name, ended, summary)

        # Transition ticket status
        env_dict = ticket.envelope.model_dump()
        env_dict["status"] = status_after
        env_dict["touched_by"] = None
        env_dict["updated_at"] = ended

        updated = Ticket(
            envelope=Envelope(**env_dict),
            payload=ticket.payload,
            result=ticket.result,
        )
        self.store.update(updated)

    def forward(
        self,
        ticket_id: str,
        reason: str,
        raw_excerpt: str,
    ) -> str:
        """
        Kill the current ticket and create a forwarded successor.

        The failed ticket is marked 'killed'. A new ticket is created with:
        - status = 'queued'
        - forwarded_from = ticket_id
        - failure_notes = original failure_notes + new FailureNote
        - same tier, same payload

        Returns the new ticket's id.

        The new ticket enters the queue immediately. The scheduler (stage 11)
        picks it up like any other queued ticket. A different agent name will
        be assigned (naming service, also stage 11).

        Raises ValueError if the ticket does not exist.
        """
        ticket = self.store.read(ticket_id)
        if ticket is None:
            raise ValueError(f"forward(): ticket {ticket_id!r} does not exist")

        now = _now()

        # Close open touch entry if one exists (agent may have died mid-work)
        if ticket.envelope.touched_by is not None:
            try:
                self.store._close_touch_entry(
                    ticket_id,
                    ticket.envelope.touched_by,
                    now,
                    "(closed by forward)",
                )
            except Exception:
                # Best-effort; don't block the forward if closing fails
                pass

        # Kill the original ticket
        env_dict = ticket.envelope.model_dump()
        # env_dict["status"] = "killed"
        #AI FIX
        env_dict["status"] = "forwarded"
        env_dict["touched_by"] = None
        env_dict["updated_at"] = now

        killed = Ticket(
            envelope=Envelope(**env_dict),
            payload=ticket.payload,
            result=ticket.result,
        )
        self.store.update(killed)

        # Build the new failure note
        # Use pre-kill touched_by — env_dict already has touched_by=None on the killed copy.
        new_note = FailureNote(
            ticket_id=ticket_id,
            agent=ticket.envelope.touched_by or "unknown",
            failed_at=now,
            reason=reason,
            raw_excerpt=raw_excerpt,
        )

        # Accumulate failure notes from the killed ticket
        prior_notes = ticket.envelope.failure_notes  # list[FailureNote]
        all_notes = prior_notes + [new_note]

        # Create the forwarded ticket
        new_id = str(uuid.uuid4())
        new_env = Envelope(
            id=new_id,
            parent_id=ticket.envelope.parent_id,
            forwarded_from=ticket_id,
            schema_version=ticket.envelope.schema_version,
            tier_name=ticket.envelope.tier_name,
            tier_level=ticket.envelope.tier_level,
            status="queued",
            collaboration_mode=ticket.envelope.collaboration_mode,
            mtap=ticket.envelope.mtap,
            created_by_agent=ticket.envelope.created_by_agent,
            failure_notes=all_notes,
        )

        new_ticket = Ticket(
            envelope=new_env,
            payload=ticket.payload,
            result=None,
        )
        self.store.create(new_ticket)

        return new_id
