# madhu/schemas/envelope.py
"""
Universal ticket envelope schema for MadCP — madhu.

Every ticket at every tier shares this envelope shape. The payload
field is tier-specific and lives in madhu/schemas/payloads.py.

Schema version is recorded explicitly on every ticket so the
migrate-on-read system knows what upgrades to apply.

Agent naming pools and tier name constants live in madhu/names.py.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from madhu.names import KRISHNAS


# ---------------------------------------------------------------------------
# Deprecated alias — remove after all code is updated to use KRISHNAS
# ---------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    if name == "ARJUNAS":
        warnings.warn(
            "ARJUNAS is renamed to KRISHNAS. Update your imports.",
            DeprecationWarning,
            stacklevel=2,
        )
        return KRISHNAS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TicketStatus(str, Enum):
    """
    All valid lifecycle states for a ticket.

    Inheriting from str means the value serialises as a plain string in JSON
    and SQLite without any special handling. ConfigDict(use_enum_values=True)
    ensures the string value — not the enum member — is stored on the model.

    Terminal states (nothing further happens to the ticket):
        done, failed, killed, aborted

    Transitional states:
        queued, touched, in_progress, forwarded
    """
    QUEUED      = "queued"       # waiting to be picked up by a worker
    TOUCHED     = "touched"      # agent has acquired it; work not yet started
    IN_PROGRESS = "in_progress"  # agent is actively working
    DONE        = "done"         # completed successfully — terminal
    FAILED      = "failed"       # terminal failure, not forwarded — terminal
    KILLED      = "killed"       # externally terminated (operator/timeout) — terminal
    FORWARDED   = "forwarded"    # failed; a new ticket was created in its place
    ABORTED     = "aborted"      # forward chain exceeded max_forwards — terminal


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class TouchEntry(BaseModel):
    """
    One record of an agent touching a ticket.

    Written when an agent acquires a ticket. The 'ended' and 'summary'
    fields are filled in when the agent releases. The full list of
    TouchEntry objects on a ticket forms its complete audit trail —
    you can reconstruct exactly who worked on it and for how long.
    """
    model_config = ConfigDict(use_enum_values=True)

    agent:   str
    started: datetime
    # ended:   datetime | None = None
    ended: str | None = None
    summary: str | None = None


class FailureNote(BaseModel):
    """
    One failure record in a ticket's lineage.

    When a ticket is forwarded, the new ticket inherits all prior failure
    notes and appends a new one. This list is strictly append-only —
    older entries are never overwritten — so the complete failure history
    of a forwarding chain is always visible on the latest ticket.
    """
    model_config = ConfigDict(use_enum_values=True)

    ticket_id:   str       # the ticket that failed (not the newly created one)
    agent:       str
    failed_at:   datetime
    reason:      str
    raw_excerpt: str = ""  # first ~500 chars of bad output, for debugging


class Result(BaseModel):
    """
    The output produced when a ticket reaches a terminal success state.

    'data' is intentionally untyped at the envelope level — the caller
    knows what shape to expect based on the payload type that was submitted.
    For function_spec payloads this will be a string of Python source code.
    """
    model_config = ConfigDict(use_enum_values=True)

    status:      str        # "success" | "failure"
    data:        Any
    produced_at: datetime
    by_agent:    str


# ---------------------------------------------------------------------------
# Helpers — extracted so tests can monkeypatch them cleanly
# To patch: monkeypatch.setattr("madhu.schemas.envelope._now", ...)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Return current UTC time, timezone-aware."""
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    """Return a new UUID4 string."""
    return str(uuid4())


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """
    The outer wrapper present on every ticket at every tier.

    The payload travels alongside as a separate field on Ticket — the
    envelope layer is deliberately ignorant of tier-specific payload shapes.
    This lets the touch manager, scheduler, and store all operate on tickets
    without importing any payload schemas.

    'param-aatma' is the internal name for the external orchestrator
    (Claude Code / Opus). From MadCP's perspective, all inbound tickets
    come from param-aatma regardless of what is actually on the other end.
    """
    model_config = ConfigDict(use_enum_values=True)

    # Identity
    id:             str = Field(default_factory=_new_uuid)
    parent_id:      str | None = None
    forwarded_from: str | None = None
    schema_version: str = "1.0"

    # Tier routing — tier_name must be a value from KRISHNAS
    tier_name:      str
    tier_level:     int

    # State
    status:               TicketStatus = TicketStatus.QUEUED
    collaboration_mode:   str = "solo"
    mtap:                 bool = True   # leaf workers are ephemeral by default

    # Timestamps
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # Ownership
    created_by_agent:  str = "param-aatma"  # external orchestrator identity
    assigned_to_agent: str | None = None
    touched_by:        str | None = None

    # History — both lists are strictly append-only
    touch_history:  list[TouchEntry]  = Field(default_factory=list)
    failure_notes:  list[FailureNote] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level Ticket
# ---------------------------------------------------------------------------


class Ticket(BaseModel):
    """
    The complete ticket object as stored in SQLite and passed between
    internal components.

    payload is a raw dict — payload validation is the job of the tier's
    worker, which knows what schema to expect. The store and scheduler
    never inspect payload contents.
    """
    model_config = ConfigDict(use_enum_values=True)

    envelope: Envelope
    payload:  dict[str, Any]
    result:   Result | None = None