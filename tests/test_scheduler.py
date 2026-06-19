# tests/test_scheduler.py
from __future__ import annotations
import time

"""
Tests for madhu/scheduler.py — Scheduler and _lineage_path.

Covers:
- _lineage_path(): two-tier prefix, hyphenated tier, full path format
- Scheduler.__init__(): _active empty, _running False
- Scheduler._reap_dead(): dead process removed, live process kept, zombie joined
- Scheduler._dispatch_queued(): one ticket dispatched, max_parallel respected,
  tier with no worker_module skipped, unknown tier skipped, already-active skipped
- Scheduler.run() + shutdown(): loop exits after shutdown()
- MTap: each ticket gets its own Process (no reuse)

Does NOT cover:
- Stage 12 (MCP server): scheduler started in background thread
- Stage 14 (failure forwarding): aborted status, max_forwards limit
- Stage 13 (JSONL log): log entries from scheduler events
- Real Ollama calls — workers are mocked/stubbed throughout
"""

import multiprocessing
import threading
import time
import uuid
from unittest.mock import MagicMock, patch, call

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
) -> Ticket:
    return Ticket(
        envelope=Envelope(
            id=ticket_id or str(uuid.uuid4()),
            tier_name=tier_name,
            tier_level=tier_level,
            status=status,
            created_by_agent="param-aatma",
        ),
        payload={"type": "function_spec", "function_name": "stub"},
    )


def make_tier_config(**kwargs) -> TierConfig:
    defaults = dict(
        tier_name="Hamsa",
        tier_level=2,
        mtap=True,
        max_parallel=2,
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


def make_registry(*configs: TierConfig) -> MagicMock:
    """Return a mock TierRegistry backed by the provided configs."""
    registry = MagicMock(spec=TierRegistry)
    config_map = {c.tier_name: c for c in configs}
    registry.get.side_effect = lambda name: config_map[name]
    registry.list_active.return_value = sorted(
        configs, key=lambda c: c.tier_level
    )
    return registry


def make_naming_service(names: list[str] = None) -> MagicMock:
    """Return a mock NamingService that returns names from the list in order."""
    ns = MagicMock()
    if names:
        ns.generate.side_effect = names
    else:
        ns.generate.return_value = "vasishtha"
    return ns


# ---------------------------------------------------------------------------
# _lineage_path
# ---------------------------------------------------------------------------

def make_real_registry_for_lineage() -> TierRegistry:
    """Build a real TierRegistry backed by the actual YAML configs."""
    from pathlib import Path
    configs_dir = Path(__file__).parent.parent / "madhu" / "tiers" / "configs"
    return TierRegistry(configs_dir)


def test_lineage_prefix_two_tiers():
    """Adi Purusha (level 1) + Hamsa (level 2) → prefix 'AdHa'."""
    registry = make_real_registry_for_lineage()
    result = _lineage_path(registry, "Hamsa", "vasishtha")
    assert result.startswith("AdHa-")


def test_lineage_full_path():
    """Full path format is {prefix}-{agent_name}."""
    registry = make_real_registry_for_lineage()
    result = _lineage_path(registry, "Hamsa", "vasishtha")
    assert result == "AdHa-vasishtha"


def test_lineage_prefix_hyphenated_tier():
    """Hyphenated tier name uses first word only: Nara-Narayana → Na."""
    # Build a mock registry with a hyphenated tier
    t1 = TierConfig(tier_name="Adi Purusha", tier_level=1)
    t2 = TierConfig(tier_name="Nara-Narayana", tier_level=5)
    t3 = make_tier_config(tier_name="Hamsa", tier_level=24)
    registry = make_registry(t1, t2, t3)
    result = _lineage_path(registry, "Hamsa", "vasishtha")
    # Prefix: Ad (Adi) + Na (Nara) + Ha (Hamsa) = AdNaHa
    assert result == "AdNaHa-vasishtha"


def test_lineage_unknown_tier_returns_agent_name():
    """Unknown tier_name → returns agent_name without prefix (defensive)."""
    registry = make_real_registry_for_lineage()
    result = _lineage_path(registry, "UnknownTier", "vasishtha")
    assert result == "vasishtha"


# ---------------------------------------------------------------------------
# Scheduler.__init__
# ---------------------------------------------------------------------------

def test_scheduler_init():
    """_active is empty dict, _running is False after init."""
    store = MagicMock(spec=TicketStore)
    registry = MagicMock(spec=TierRegistry)
    ns = MagicMock()
    scheduler = Scheduler(store, registry, ns)
    assert scheduler._active == {}
    assert scheduler._running is False


# ---------------------------------------------------------------------------
# Scheduler._reap_dead
# ---------------------------------------------------------------------------

def test_reap_dead_removes_finished_process():
    """Dead process is removed from _active and joined."""
    store = MagicMock(spec=TicketStore)
    scheduler = Scheduler(store, MagicMock(), MagicMock())

    dead_proc = MagicMock()
    dead_proc.is_alive.return_value = False
    live_proc = MagicMock()
    live_proc.is_alive.return_value = True

    # scheduler._active = {"t-dead": dead_proc, "t-live": live_proc}
    scheduler._active = {
    "t-dead": (dead_proc, time.monotonic(), "Hamsa"),
    "t-live": (live_proc, time.monotonic(), "Hamsa"),
}
    scheduler._reap_dead()

    assert "t-dead" not in scheduler._active
    assert "t-live" in scheduler._active
    dead_proc.join.assert_called_once_with(timeout=0)
    live_proc.join.assert_not_called()


# ---------------------------------------------------------------------------
# Scheduler._dispatch_queued
# ---------------------------------------------------------------------------

def test_dispatch_queued_spawns_process(tmp_path):
    """One queued ticket → one Process spawned."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    ticket = make_ticket(ticket_id="t-dispatch-001")
    store.create(ticket)

    hamsa_config = make_tier_config()
    registry = make_registry(hamsa_config)
    ns = make_naming_service(["vasishtha"])

    scheduler = Scheduler(store, registry, ns)

    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        mock_proc = MagicMock()
        mock_proc_cls.return_value = mock_proc
        scheduler._dispatch_queued()

    mock_proc.start.assert_called_once()
    assert "t-dispatch-001" in scheduler._active


def test_dispatch_respects_max_parallel(tmp_path):
    """Three tickets queued, max_parallel=2 → exactly 2 spawned."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    for i in range(3):
        store.create(make_ticket(ticket_id=f"t-par-00{i}"))

    hamsa_config = make_tier_config(max_parallel=2)
    registry = make_registry(hamsa_config)
    ns = make_naming_service(["vasishtha", "agastya", "atri"])

    scheduler = Scheduler(store, registry, ns)

    spawned = []
    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        def make_proc(*args, **kwargs):
            proc = MagicMock()
            proc.is_alive.return_value = True
            spawned.append(proc)
            return proc
        mock_proc_cls.side_effect = make_proc
        scheduler._dispatch_queued()

    assert len(spawned) == 2


def test_dispatch_skips_tier_without_worker(tmp_path):
    """Tier with no worker_module configured is skipped silently."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    store.create(make_ticket(ticket_id="t-skip-001", tier_name="Adi Purusha", tier_level=1))

    adi_config = TierConfig(
        tier_name="Adi Purusha",
        tier_level=1,
        worker_module=None,
        worker_entrypoint=None,
    )
    registry = make_registry(adi_config)
    ns = make_naming_service()
    scheduler = Scheduler(store, registry, ns)

    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        scheduler._dispatch_queued()

    mock_proc_cls.assert_not_called()


def test_dispatch_skips_already_active(tmp_path):
    """Ticket already in _active is not dispatched again."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    store.create(make_ticket(ticket_id="t-active-001"))

    hamsa_config = make_tier_config()
    registry = make_registry(hamsa_config)
    ns = make_naming_service()
    scheduler = Scheduler(store, registry, ns)

    existing_proc = MagicMock()
    existing_proc.is_alive.return_value = True
    # scheduler._active["t-active-001"] = existing_proc
    scheduler._active["t-active-001"] = (existing_proc, time.monotonic(), "Hamsa")

    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        scheduler._dispatch_queued()

    mock_proc_cls.assert_not_called()


def test_dispatch_skips_unknown_tier(tmp_path):
    """Ticket with unknown tier_name is skipped; loop continues."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    store.create(make_ticket(ticket_id="t-unknown-001", tier_name="Vamana", tier_level=16))
    store.create(make_ticket(ticket_id="t-known-001", tier_name="Hamsa", tier_level=2))

    hamsa_config = make_tier_config()
    registry = MagicMock(spec=TierRegistry)
    registry.get.side_effect = lambda name: (
        hamsa_config if name == "Hamsa" else (_ for _ in ()).throw(KeyError(name))
    )
    registry.list_active.return_value = [hamsa_config]

    ns = make_naming_service(["vasishtha"])
    scheduler = Scheduler(store, registry, ns)

    spawned = []
    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        def make_proc(*args, **kwargs):
            proc = MagicMock()
            proc.is_alive.return_value = True
            spawned.append(proc)
            return proc
        mock_proc_cls.side_effect = make_proc
        scheduler._dispatch_queued()

    # Only the known-tier ticket is dispatched
    assert len(spawned) == 1
    assert "t-known-001" in scheduler._active
    assert "t-unknown-001" not in scheduler._active


def test_dispatch_mtap_one_process_per_ticket(tmp_path):
    """Each ticket gets its own Process — processes are never reused."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    store.create(make_ticket(ticket_id="t-mtap-001"))
    store.create(make_ticket(ticket_id="t-mtap-002"))

    hamsa_config = make_tier_config(max_parallel=2)
    registry = make_registry(hamsa_config)
    ns = make_naming_service(["vasishtha", "agastya"])
    scheduler = Scheduler(store, registry, ns)

    processes = []
    with patch("madhu.scheduler.multiprocessing.Process") as mock_proc_cls:
        def make_proc(*args, **kwargs):
            proc = MagicMock()
            proc.is_alive.return_value = True
            processes.append(proc)
            return proc
        mock_proc_cls.side_effect = make_proc
        scheduler._dispatch_queued()

    assert len(processes) == 2
    # Each Process is a distinct object
    assert processes[0] is not processes[1]


# ---------------------------------------------------------------------------
# Scheduler.run() + shutdown()
# ---------------------------------------------------------------------------

def test_scheduler_shutdown_exits_loop(tmp_path):
    """shutdown() causes run() to exit within a reasonable time."""
    db_path = str(tmp_path / "test.db")
    store = TicketStore(db_path)
    registry = MagicMock(spec=TierRegistry)
    registry.list_active.return_value = []
    ns = MagicMock()

    scheduler = Scheduler(store, registry, ns)

    # Patch _dispatch_queued to do nothing (no real tickets)
    scheduler._dispatch_queued = MagicMock()

    thread = threading.Thread(target=scheduler.run, daemon=True)
    thread.start()
    time.sleep(0.1)  # let the loop start
    scheduler.shutdown()
    thread.join(timeout=2.0)

    assert not thread.is_alive(), "scheduler.run() did not exit after shutdown()"
