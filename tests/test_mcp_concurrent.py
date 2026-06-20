# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLAights Reserved. See LICENSE.
# tests/test_mcp_concurrent.py
"""
Concurrent submit_ticket verification — Stage 12.5.

Verifies that two simultaneous submit_ticket calls:
  - Do not cross wires (each returns its own distinct ticket dict)
  - Produce distinct ticket IDs (UUID4)
  - Return without exceptions under concurrent async execution
  - Both tickets are independently written to the store

Poll phase is mocked — the test is about isolation of the
validate → create → poll path, not scheduler behaviour.
Scheduler concurrency is exercised at integration checkpoint C3.

Does NOT cover:
  health_check tool (Stage 12.5 smoke test)
  Race conditions inside TicketStore._lock (covered by test_touch.py)
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest

import server
from madhu.store.sqlite import TicketStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope() -> dict:
    return {"tier_name": "Hamsa", "tier_level": 2}


def _make_payload(fn_name: str = "add_one") -> dict:
    """Return a minimal valid FunctionSpec payload."""
    return {
        "type": "function_spec",
        "function_name": fn_name,
        "signature": f"def {fn_name}(n: int) -> int:",
        "docstring": f"Returns n + 1.",
        "constraints": [],
        # "examples": [{"input": 1, "output": 2}],
        "examples": [{"input": "1", "output": "2"}],
        "imports_allowed": [],
    }


def _fake_poll(store: TicketStore, ticket_id: str, timeout: float = 600.0):
    """Fake poll: returns the ticket exactly as written to the store.

    Status is 'queued' — intentional. The concurrent test verifies isolation,
    not terminal-state semantics. Terminal-state polling is covered in
    test_mcp_helpers.py::TestPollUntilTerminal.
    """
    return store.read(ticket_id)


# ---------------------------------------------------------------------------
# Concurrent submission test
# ---------------------------------------------------------------------------

async def test_concurrent_submit_no_crossed_wires():
    """Two concurrent submit_ticket calls must not cross wires.

    Uses asyncio.gather to interleave both coroutines on the same event loop.
    poll_until_terminal is patched so the test does not need a live scheduler.
    store.create is called on a real :memory: TicketStore to exercise the
    actual threading.Lock under concurrent access.
    """
    real_store = TicketStore(":memory:")

    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True

    with (
        patch.object(server, "_store", real_store),
        patch.object(server, "_scheduler_thread", mock_thread),
        patch("server.poll_until_terminal", side_effect=_fake_poll),
        # patch("server.poll_until_terminal", side_effect=_fake_poll),
    ):
        results = await asyncio.gather(
            server.submit_ticket(_make_envelope(), _make_payload("add_one")),
            server.submit_ticket(_make_envelope(), _make_payload("subtract_one")),
        )

    assert len(results) == 2

    r0, r1 = results

    # Neither call returned an error dict.
    assert "error" not in r0, f"first call returned error: {r0.get('error')}"
    assert "error" not in r1, f"second call returned error: {r1.get('error')}"

    # Both results carry an envelope with an id.
    id0 = r0["envelope"]["id"]
    id1 = r1["envelope"]["id"]

    # IDs are distinct — no crossed wires.
    assert id0 != id1, f"both calls returned the same ticket id: {id0!r}"

    # Both IDs are valid UUID4.
    assert uuid.UUID(id0).version == 4, f"id0 is not UUID4: {id0!r}"
    assert uuid.UUID(id1).version == 4, f"id1 is not UUID4: {id1!r}"

    # Both tickets were written to the store independently.
    stored0 = real_store.read(id0)
    stored1 = real_store.read(id1)
    assert stored0 is not None, f"ticket {id0!r} not found in store after submit"
    assert stored1 is not None, f"ticket {id1!r} not found in store after submit"

    # Payloads are not swapped — each ticket holds its own function name.
    fn0 = r0["payload"]["function_name"]
    fn1 = r1["payload"]["function_name"]
    assert {fn0, fn1} == {"add_one", "subtract_one"}, (
        f"payload function names crossed: got {fn0!r} and {fn1!r}"
    )
