# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# tests/test_touch.py
from __future__ import annotations

"""
Tests for madhu/store/touch.py — TouchManager.

Covers:
- acquire() succeeds on a queued ticket
- acquire() returns False on non-queued ticket
- acquire() is atomic: two concurrent callers, exactly one succeeds
- release() closes touch entry and sets status
- release() raises on wrong agent
- release() raises on invalid status_after
- forward() kills original, creates new queued ticket
- forward() appends FailureNote to new ticket
- forward() chain: failure_notes accumulates across two forwards
- forward() with different agents on successive tickets

Does NOT cover:
- Stage 11 (scheduler): max_parallel enforcement, agent assignment
- Stage 14 (failure forwarding): aborted status, max_forwards limit
- Stage 9 (Gemma worker): end-to-end acquire→work→release cycle
"""

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from madhu.schemas.envelope import Envelope, FailureNote, Result, Ticket, TouchEntry
from madhu.store.sqlite import TicketStore
from madhu.store.touch import TouchManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ticket(ticket_id: str = None, status: str = "queued", tier: str = "Hamsa") -> Ticket:
    """Return a minimal valid Ticket."""
    return Ticket(
        envelope=Envelope(
            id=ticket_id or str(uuid.uuid4()),
            tier_name=tier,
            tier_level=2,
            status=status,
            created_by_agent="param-aatma",
        ),
        payload={"type": "function_spec", "function_name": "stub"},
        result=None,
    )


@pytest.fixture
def store() -> TicketStore:
    """In-memory TicketStore, function-scoped."""
    return TicketStore(":memory:")


@pytest.fixture
def tm(store) -> TouchManager:
    """TouchManager backed by in-memory store."""
    return TouchManager(store)


def _insert(store: TicketStore, **kwargs) -> Ticket:
    """Insert a ticket and return it."""
    t = make_ticket(**kwargs)
    store.create(t)
    return t


# ---------------------------------------------------------------------------
# acquire()
# ---------------------------------------------------------------------------

def test_acquire_succeeds_on_queued(tm, store):
    """acquire() returns True and transitions status to touched."""
    t = _insert(store, ticket_id="t-001")
    result = tm.acquire("t-001", "vasishtha")
    assert result is True
    refreshed = store.read("t-001")
    assert refreshed.envelope.status == "touched"
    assert refreshed.envelope.assigned_to_agent == "vasishtha"
    assert refreshed.envelope.touched_by == "vasishtha"


def test_acquire_returns_false_on_nonexistent(tm):
    """acquire() returns False if ticket does not exist."""
    assert tm.acquire("no-such-ticket", "vasishtha") is False


def test_acquire_returns_false_on_already_touched(tm, store):
    """acquire() returns False if ticket is already touched."""
    _insert(store, ticket_id="t-002")
    tm.acquire("t-002", "vasishtha")
    # Second acquire by a different agent must fail
    assert tm.acquire("t-002", "agastya") is False


def test_acquire_returns_false_on_done(tm, store):
    """acquire() returns False for terminal-status tickets."""
    _insert(store, ticket_id="t-003", status="done")
    assert tm.acquire("t-003", "vasishtha") is False


def test_acquire_appends_touch_entry(tm, store):
    """acquire() appends a TouchEntry to touch_history."""
    _insert(store, ticket_id="t-004")
    tm.acquire("t-004", "vasishtha")
    refreshed = store.read("t-004")
    assert len(refreshed.envelope.touch_history) == 1
    assert refreshed.envelope.touch_history[0].agent == "vasishtha"


def test_acquire_atomic_concurrent(store):
    """
    Two threads acquiring the same queued ticket: exactly one succeeds.

    Uses a real TicketStore (in-memory SQLite). Both threads call acquire()
    simultaneously via a threading.Barrier. The results list must contain
    exactly one True and one False.
    """
    _insert(store, ticket_id="t-concurrent")
    results = []
    barrier = threading.Barrier(2)

    def try_acquire(agent_name):
        tm = TouchManager(store)
        barrier.wait()  # both threads start at the same moment
        results.append(tm.acquire("t-concurrent", agent_name))

    t1 = threading.Thread(target=try_acquire, args=("vasishtha",))
    t2 = threading.Thread(target=try_acquire, args=("agastya",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [False, True]


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------

def test_release_sets_done(tm, store):
    """release() closes touch entry and sets status to done."""
    _insert(store, ticket_id="t-005")
    tm.acquire("t-005", "vasishtha")
    tm.release("t-005", "vasishtha", "wrote function", "done")
    refreshed = store.read("t-005")
    assert refreshed.envelope.status == "done"
    assert refreshed.envelope.touched_by is None


def test_release_closes_touch_entry(tm, store):
    """release() updates the touch entry with summary."""
    _insert(store, ticket_id="t-006")
    tm.acquire("t-006", "vasishtha")
    tm.release("t-006", "vasishtha", "all tests pass", "done")
    refreshed = store.read("t-006")
    entry = refreshed.envelope.touch_history[0]
    assert entry.summary == "all tests pass"
    assert entry.agent == "vasishtha"
    assert entry.ended is not None
    assert entry.summary == "all tests pass"


def test_release_raises_wrong_agent(tm, store):
    """release() raises ValueError if called by a different agent."""
    _insert(store, ticket_id="t-007")
    tm.acquire("t-007", "vasishtha")
    with pytest.raises(ValueError, match="not currently held by"):
        tm.release("t-007", "agastya", "summary", "done")


def test_release_raises_invalid_status(tm, store):
    """release() raises ValueError for non-terminal status_after."""
    _insert(store, ticket_id="t-008")
    tm.acquire("t-008", "vasishtha")
    with pytest.raises(ValueError, match="must be one of"):
        tm.release("t-008", "vasishtha", "summary", "queued")


def test_release_raises_nonexistent_ticket(tm):
    """release() raises ValueError if ticket does not exist."""
    with pytest.raises(ValueError, match="does not exist"):
        tm.release("no-such-ticket", "vasishtha", "summary", "done")


# ---------------------------------------------------------------------------
# forward()
# ---------------------------------------------------------------------------

def test_forward_sets_original_to_forwarded(tm, store):
    """forward() sets original ticket status to 'forwarded', not 'killed'."""
    _insert(store, ticket_id="t-009")
    tm.acquire("t-009", "vasishtha")
    tm.forward("t-009", "vasishtha", "Gemma returned junk", "def foo(): ...")
    original = store.read("t-009")
    assert original.envelope.status == "forwarded"


def test_forward_creates_new_queued_ticket(tm, store):
    """forward() creates a new ticket with status=queued."""
    _insert(store, ticket_id="t-010")
    tm.acquire("t-010", "vasishtha")
    new_id = tm.forward("t-010", "vasishtha", "parse error", "...")
    new_ticket = store.read(new_id)
    assert new_ticket is not None
    assert new_ticket.envelope.status == "queued"


def test_forward_links_via_forwarded_from(tm, store):
    """New ticket's forwarded_from points to the killed ticket."""
    _insert(store, ticket_id="t-011")
    tm.acquire("t-011", "vasishtha")
    new_id = tm.forward("t-011", "vasishtha", "reason", "excerpt")
    new_ticket = store.read(new_id)
    assert new_ticket.envelope.forwarded_from == "t-011"


def test_forward_appends_failure_note(tm, store):
    """New ticket has one FailureNote from the forward."""
    _insert(store, ticket_id="t-012")
    tm.acquire("t-012", "vasishtha")
    new_id = tm.forward("t-012", "vasishtha", "Gemma multi-function output", "def a(): ...\ndef b(): ...")
    new_ticket = store.read(new_id)
    assert len(new_ticket.envelope.failure_notes) == 1
    note = new_ticket.envelope.failure_notes[0]
    assert note.reason == "Gemma multi-function output"
    assert note.ticket_id == "t-012"
    assert note.agent == "vasishtha"


def test_forward_chain_accumulates_failure_notes(tm, store):
    """
    Two forwards: second ticket carries 2 failure_notes.

    Simulates the retry chain: t-013 → t-014 → t-015.
    t-015 has failure_notes from both prior attempts.
    """
    _insert(store, ticket_id="t-013")
    tm.acquire("t-013", "vasishtha")
    second_id = tm.forward("t-013", "vasishtha", "first failure", "bad output 1")

    tm.acquire(second_id, "agastya")
    third_id = tm.forward(second_id, "agastya", "second failure", "bad output 2")

    third = store.read(third_id)
    assert len(third.envelope.failure_notes) == 2
    assert third.envelope.failure_notes[0].reason == "first failure"
    assert third.envelope.failure_notes[1].reason == "second failure"


def test_forward_preserves_payload(tm, store):
    """Forwarded ticket carries the same payload as the original."""
    _insert(store, ticket_id="t-014")
    tm.acquire("t-014", "vasishtha")
    new_id = tm.forward("t-014", "vasishtha", "reason", "excerpt")
    original = store.read("t-014")
    new_ticket = store.read(new_id)
    assert new_ticket.payload == original.payload


def test_forward_raises_nonexistent(tm):
    """forward() raises ValueError if ticket does not exist."""
    with pytest.raises(ValueError, match="does not exist"):
        tm.forward("no-such-ticket", "vasishtha", "reason", "excerpt")


def test_forward_different_agents_on_chain(tm, store):
    """
    Successive forwarded tickets can be acquired by different agents.
    This is the expected pattern — same agent must not retry.
    """
    _insert(store, ticket_id="t-015")
    tm.acquire("t-015", "vasishtha")
    second_id = tm.forward("t-015", "vasishtha", "reason", "excerpt")

    # agastya picks up the forwarded ticket — different agent, should succeed
    result = tm.acquire(second_id, "agastya")
    assert result is True