
# tests/test_scheduler_resilience.py
from __future__ import annotations

"""
Tests for Stage 11.5 scheduler resilience features.

Covers:
- Janitor: orphaned touched ticket is re-queued with FailureNote on startup
- Janitor: orphaned in_progress ticket is re-queued
- Janitor: done/queued tickets are not touched
- Timeout: worker exceeding timeout is SIGKILLed and ticket forwarded
- Timeout: worker within timeout is not killed
- Graceful shutdown: in-flight workers get grace period
- Graceful shutdown: workers still alive after grace are SIGTERMed/SIGKILLed
- Shutdown: tickets stuck in non-terminal state are marked killed

Does NOT cover:
- Stage 13 (JSONL log): log file content format
- Stage 14 (failure forwarding): aborted status, max_forwards limit
- Real Ollama calls
"""

import multiprocessing
import os
import signal
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from madhu.schemas.envelope import Envelope, Ticket
from madhu.scheduler import Scheduler, _lineage_path
from madhu.store.sqlite import TicketStore
from madhu.tiers.registry import TierConfig, TierRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ticket(
    ticket_id: str = None,
    status: str = "queued",
    tier_name: str = "Hamsa",
    tier_level: int = 2,
    touched_by: str | None = None,
    assigned_to_agent: str | None = None,
) -> Ticket:
    return Ticket(
        envelope=Envelope(
            id=ticket_id or str(uuid.uuid4()),
            tier_name=tier_name,
            tier_level=tier_level,
            status=status,
            created_by_agent="param-aatma",
            touched_by=touched_by,
            assigned_to_agent=assigned_to_agent,
        ),
        payload={"type": "function_spec", "function_name": "stub"},
    )


def make_tier_config(**kwargs) -> TierConfig:
    defaults = dict(
        tier_name="Hamsa",
        tier_level=2,
        mtap=True,
        max_parallel=2,
        worker_timeout_seconds=180,
        worker_module="madhu.workers.hamsa",
        worker_entrypoint="run_worker",
        provider="ollama",
        provider_config={
            "model": "test-model",
            "endpoint": "http://localhost:11434",
            "temperature": 0.2,
            "timeout": 30,
        },
    )
    defaults.update(kwargs)
    return TierConfig(**defaults)


def make_scheduler(store, tier_config=None, grace=0.5) -> Scheduler:
    """Return a Scheduler with mock registry and naming service."""
    config = tier_config or make_tier_config()
    registry = MagicMock(spec=TierRegistry)
    registry.get.return_value = config
    registry.list_active.return_value = [config]
    ns = MagicMock()
    ns.generate.return_value = "vasishtha"
    return Scheduler(store, registry, ns, shutdown_grace_seconds=grace)


# ---------------------------------------------------------------------------
# Janitor tests
# ---------------------------------------------------------------------------

def test_janitor_requeues_touched_ticket(tmp_path):
    """Janitor re-queues a ticket left in 'touched' status."""
    store = TicketStore(str(tmp_path / "test.db"))
    ticket = make_ticket(
        ticket_id="t-orphan-001",
        status="touched",
        touched_by="vasishtha",
        assigned_to_agent="vasishtha",
    )
    store.create(ticket)

    scheduler = make_scheduler(store)
    scheduler._janitor()

    refreshed = store.read("t-orphan-001")
    assert refreshed.envelope.status == "queued"
    assert refreshed.envelope.touched_by is None
    assert refreshed.envelope.assigned_to_agent is None
    assert len(refreshed.envelope.failure_notes) == 1
    assert "orphaned" in refreshed.envelope.failure_notes[0].reason


def test_janitor_requeues_in_progress_ticket(tmp_path):
    """Janitor re-queues a ticket left in 'in_progress' status."""
    store = TicketStore(str(tmp_path / "test.db"))
    ticket = make_ticket(
        ticket_id="t-orphan-002",
        status="in_progress",
        touched_by="agastya",
    )
    store.create(ticket)

    scheduler = make_scheduler(store)
    scheduler._janitor()

    refreshed = store.read("t-orphan-002")
    assert refreshed.envelope.status == "queued"


def test_janitor_does_not_touch_done_tickets(tmp_path):
    """Janitor leaves done/queued/killed tickets alone."""
    store = TicketStore(str(tmp_path / "test.db"))
    for status in ("done", "queued", "killed", "aborted"):
        store.create(make_ticket(ticket_id=f"t-{status}", status=status))

    scheduler = make_scheduler(store)
    scheduler._janitor()

    for status in ("done", "killed", "aborted"):
        refreshed = store.read(f"t-{status}")
        assert refreshed.envelope.status == status, f"Status changed for {status}"

    # queued ticket stays queued, no failure notes added
    queued = store.read("t-queued")
    assert queued.envelope.status == "queued"
    assert len(queued.envelope.failure_notes) == 0


def test_janitor_appends_failure_note_with_correct_agent(tmp_path):
    """FailureNote agent field matches the touched_by value."""
    store = TicketStore(str(tmp_path / "test.db"))
    ticket = make_ticket(
        ticket_id="t-orphan-003",
        status="touched",
        touched_by="bharadwaja",
    )
    store.create(ticket)

    scheduler = make_scheduler(store)
    scheduler._janitor()

    refreshed = store.read("t-orphan-003")
    assert refreshed.envelope.failure_notes[0].agent == "bharadwaja"


# ---------------------------------------------------------------------------
# Worker timeout tests
# ---------------------------------------------------------------------------

def test_timeout_kills_slow_worker(tmp_path):
    """Worker running longer than timeout is SIGKILLed and ticket forwarded."""
    store = TicketStore(str(tmp_path / "test.db"))
    ticket = make_ticket(ticket_id="t-timeout-001", status="queued")
    store.create(ticket)

    # Manually acquire the touch so the ticket is in 'touched' state
    from madhu.store.touch import TouchManager
    tm = TouchManager(store)
    tm.acquire("t-timeout-001", "vasishtha")

    scheduler = make_scheduler(store, make_tier_config(worker_timeout_seconds=1))

    # Inject a fake process that is alive but started 10s ago
    fake_proc = MagicMock()
    fake_proc.is_alive.return_value = True
    fake_proc.pid = 99999
    scheduler._active["t-timeout-001"] = (fake_proc, time.monotonic() - 10, "Hamsa")

    with patch("os.kill") as mock_kill, \
         patch.object(scheduler._touch_manager, "forward") as mock_forward:
        scheduler._check_timeouts()

    mock_kill.assert_called_once_with(99999, signal.SIGKILL)
    mock_forward.assert_called_once()
    args, kwargs = mock_forward.call_args
    ticket_arg = args[0] if args else kwargs.get("ticket_id")
    reason_arg = args[1] if len(args) > 1 else kwargs.get("reason", "")
    assert ticket_arg == "t-timeout-001"
    assert "timeout" in reason_arg
    assert "t-timeout-001" not in scheduler._active

def test_timeout_does_not_kill_fast_worker(tmp_path):
    """Worker within timeout is left alone."""
    store = TicketStore(str(tmp_path / "test.db"))
    scheduler = make_scheduler(store, make_tier_config(worker_timeout_seconds=180))

    fake_proc = MagicMock()
    fake_proc.is_alive.return_value = True
    fake_proc.pid = 99998
    # Started just now — well within 180s timeout
    scheduler._active["t-fast-001"] = (fake_proc, time.monotonic(), "Hamsa")

    with patch("os.kill") as mock_kill:
        scheduler._check_timeouts()

    mock_kill.assert_not_called()
    assert "t-fast-001" in scheduler._active


# ---------------------------------------------------------------------------
# Graceful shutdown tests
# ---------------------------------------------------------------------------

def test_graceful_shutdown_waits_for_workers(tmp_path):
    """Workers that finish within grace period are not force-killed."""
    store = TicketStore(str(tmp_path / "test.db"))
    scheduler = make_scheduler(store, grace=2.0)

    # Fake process that "finishes" after first is_alive() call
    call_count = [0]
    fake_proc = MagicMock()
    def is_alive_side_effect():
        call_count[0] += 1
        return call_count[0] <= 1  # alive on first call, dead after
    fake_proc.is_alive.side_effect = is_alive_side_effect
    fake_proc.pid = 99997

    scheduler._active["t-grace-001"] = (fake_proc, time.monotonic(), "Hamsa")

    with patch("os.kill") as mock_kill:
        scheduler._graceful_shutdown()

    # Worker finished within grace — no kill signal
    mock_kill.assert_not_called()
    assert scheduler._active == {}


def test_graceful_shutdown_kills_stubborn_workers(tmp_path):
    """Workers still alive after grace period receive SIGTERM then SIGKILL."""
    store = TicketStore(str(tmp_path / "test.db"))
    # Very short grace so test runs fast
    scheduler = make_scheduler(store, grace=0.1)

    fake_proc = MagicMock()
    fake_proc.is_alive.return_value = True  # never finishes
    fake_proc.pid = 99996

    ticket = make_ticket(ticket_id="t-stubborn-001", status="in_progress")
    store.create(ticket)
    scheduler._active["t-stubborn-001"] = (fake_proc, time.monotonic(), "Hamsa")

    kill_signals = []
    with patch("os.kill", side_effect=lambda pid, sig: kill_signals.append(sig)), \
         patch("time.sleep"):  # speed up the 5s SIGKILL wait
        scheduler._graceful_shutdown()

    assert signal.SIGTERM in kill_signals
    assert signal.SIGKILL in kill_signals


def test_graceful_shutdown_marks_ticket_killed(tmp_path):
    """Ticket for a force-killed worker is marked killed in SQLite."""
    store = TicketStore(str(tmp_path / "test.db"))
    scheduler = make_scheduler(store, grace=0.1)

    ticket = make_ticket(ticket_id="t-mark-killed-001", status="in_progress")
    store.create(ticket)

    fake_proc = MagicMock()
    fake_proc.is_alive.return_value = True
    fake_proc.pid = 99995
    scheduler._active["t-mark-killed-001"] = (fake_proc, time.monotonic(), "Hamsa")

    with patch("os.kill"), patch("time.sleep"):
        scheduler._graceful_shutdown()

    refreshed = store.read("t-mark-killed-001")
    assert refreshed.envelope.status == "killed"


def test_shutdown_stops_run_loop(tmp_path):
    """shutdown() causes run() to exit within a reasonable time."""
    store = TicketStore(str(tmp_path / "test.db"))
    scheduler = make_scheduler(store, grace=0.1)
    scheduler._janitor = MagicMock()  # skip janitor in loop test
    scheduler._dispatch_queued = MagicMock()
    scheduler._install_signal_handler = MagicMock()  # can't set from non-main thread

    thread = threading.Thread(target=scheduler.run, daemon=True)
    thread.start()
    time.sleep(0.15)
    scheduler.shutdown()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
