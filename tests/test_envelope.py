# tests/test_envelope.py
"""
Tests for the universal envelope schema.
Covers: KRISHNAS constant, default values, status enum, JSON round-trip,
        deprecated ARJUNAS alias, mutable default isolation.
"""

import warnings
from datetime import datetime, timezone

import pytest

from madhu.names import KRISHNAS
from madhu.schemas.envelope import (
    Envelope,
    FailureNote,
    Result,
    Ticket,
    TicketStatus,
    TouchEntry,
)


# ---------------------------------------------------------------------------
# KRISHNAS constant
# ---------------------------------------------------------------------------


def test_krishnas_length():
    """Architecture doc specifies exactly 24 tier names."""
    assert len(KRISHNAS) == 24


def test_krishnas_first_and_last():
    """Adi Purusha is the highest tier; Hamsa is the leaf."""
    assert KRISHNAS[0] == "Adi Purusha"
    assert KRISHNAS[-1] == "Hamsa"


def test_arjunas_alias_emits_deprecation_warning():
    """
    The ARJUNAS name is retired. Importing it from envelope must emit
    a DeprecationWarning so any stale code is caught immediately.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import madhu.schemas.envelope as env_mod
        _ = env_mod.ARJUNAS
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


# ---------------------------------------------------------------------------
# TicketStatus enum
# ---------------------------------------------------------------------------


def test_status_enum_values():
    """All statuses from the updated architecture doc are present."""
    expected = {
        "queued", "touched", "in_progress", "done",
        "failed", "killed", "forwarded", "aborted",
    }
    actual = {s.value for s in TicketStatus}
    assert actual == expected


def test_aborted_distinct_from_killed():
    """ABORTED and KILLED are separate terminal states."""
    assert TicketStatus.ABORTED != TicketStatus.KILLED
    assert TicketStatus.ABORTED.value == "aborted"
    assert TicketStatus.KILLED.value == "killed"


def test_status_invalid_string_rejected():
    """An envelope with an unrecognised status must raise a ValidationError."""
    with pytest.raises(Exception):
        Envelope(tier_name="Hamsa", tier_level=24, status="flying")


# ---------------------------------------------------------------------------
# Envelope defaults
# ---------------------------------------------------------------------------


def test_envelope_defaults():
    """
    Default envelope values must match the v2.1 architecture spec exactly.
    Any drift here propagates silently into SQLite.
    """
    env = Envelope(tier_name="Hamsa", tier_level=24)

    assert env.schema_version == "1.0"
    assert env.status == "queued"           # use_enum_values=True → plain string
    assert env.mtap is True
    assert env.collaboration_mode == "solo"
    assert env.created_by_agent == "param-aatma"   # renamed from madhu
    assert env.assigned_to_agent is None
    assert env.touched_by is None
    assert env.touch_history == []
    assert env.failure_notes == []
    assert env.id is not None and len(env.id) == 36  # UUID4


def test_envelope_id_is_unique():
    """Two envelopes must never share an id."""
    a = Envelope(tier_name="Hamsa", tier_level=24)
    b = Envelope(tier_name="Hamsa", tier_level=24)
    assert a.id != b.id


def test_envelope_timestamps_are_utc_aware():
    """Timestamps must be timezone-aware UTC, not naive datetimes."""
    env = Envelope(tier_name="Hamsa", tier_level=24)
    assert env.created_at.tzinfo is not None
    assert env.updated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


def test_touch_entry_optional_fields_default_none():
    """ended and summary are None until the agent releases the ticket."""
    te = TouchEntry(agent="vasishtha", started=datetime.now(timezone.utc))
    assert te.ended is None
    assert te.summary is None


def test_failure_note_raw_excerpt_defaults_to_empty_string():
    """raw_excerpt defaults to empty string, not None."""
    fn = FailureNote(
        ticket_id="abc",
        agent="atri",
        failed_at=datetime.now(timezone.utc),
        reason="bad output",
    )
    assert fn.raw_excerpt == ""


def test_failure_notes_list_is_not_shared_between_instances():
    """
    Two envelopes must not share the same failure_notes list object.
    A shared mutable default is a classic Pydantic pitfall — Field(default_factory=list)
    prevents it, but this test guards against regression.
    """
    a = Envelope(tier_name="Hamsa", tier_level=24)
    b = Envelope(tier_name="Hamsa", tier_level=24)
    a.failure_notes.append(
        FailureNote(
            ticket_id="x",
            agent="atri",
            failed_at=datetime.now(timezone.utc),
            reason="test",
        )
    )
    assert len(b.failure_notes) == 0


# ---------------------------------------------------------------------------
# Ticket round-trip
# ---------------------------------------------------------------------------


def test_ticket_json_round_trip():
    """
    A Ticket serialised to JSON and deserialised must be field-for-field
    identical. This is the contract the SQLite store relies on in stage 6.
    """
    ticket = Ticket(
        envelope=Envelope(tier_name="Hamsa", tier_level=24),
        payload={"type": "function_spec", "name": "add"},
    )

    raw = ticket.model_dump_json()
    restored = Ticket.model_validate_json(raw)

    assert restored.envelope.id == ticket.envelope.id
    assert restored.envelope.tier_name == ticket.envelope.tier_name
    assert restored.envelope.created_by_agent == "param-aatma"
    assert restored.payload == ticket.payload
    assert restored.result is None


def test_ticket_with_result_round_trip():
    """Result field survives serialisation when present."""
    ticket = Ticket(
        envelope=Envelope(tier_name="Hamsa", tier_level=24),
        payload={"type": "function_spec"},
        result=Result(
            status="success",
            data="def add(a, b): return a + b",
            produced_at=datetime.now(timezone.utc),
            by_agent="vasishtha",
        ),
    )

    raw = ticket.model_dump_json()
    restored = Ticket.model_validate_json(raw)

    assert restored.result is not None
    assert restored.result.status == "success"
    assert restored.result.by_agent == "vasishtha"