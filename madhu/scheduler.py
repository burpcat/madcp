# madhu/scheduler.py
from __future__ import annotations

"""
Scheduler for MadCP.

Polls SQLite every 0.5s for queued tickets and dispatches workers as
multiprocessing.Process instances. Enforces max_parallel per tier.
Generates agent lineage paths (AdHa-vasishtha style) at spawn time.

MTap invariant: each queued ticket gets its own fresh process.
Processes are never reused across tickets.

Resilience features (Stage 11.5):
- Stale ticket janitor: on startup, re-queues tickets left in non-terminal
  states by a previous crashed scheduler run.
- Worker wall-clock timeout: kills workers that exceed their tier's
  worker_timeout_seconds and forwards the ticket.
- Graceful shutdown: SIGINT stops new dispatch; in-flight workers get a
  grace period before forced termination.

Pool exhaustion behavior: if naming_service.generate() raises (pool
exhausted), the ticket stays queued and dispatch is retried on the next
poll. This produces a log entry every 0.5s until a slot opens — expected
behavior, not a bug.
"""

import importlib
import multiprocessing
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from madhu.naming import NamingService
from madhu.schemas.envelope import Envelope, FailureNote, Ticket
from madhu.store.sqlite import TicketStore
from madhu.store.touch import TouchManager
from madhu.tiers.registry import TierConfig, TierRegistry

_POLL_INTERVAL = 0.5  # seconds — locked, not configurable in v0
_SIGKILL_GRACE = 5.0  # seconds between SIGTERM and SIGKILL on forced shutdown


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lineage path helper
# ---------------------------------------------------------------------------

def _lineage_path(
    tier_registry: TierRegistry,
    ticket_tier_name: str,
    agent_name: str,
) -> str:
    """
    Compute the full agent lineage path for display in dashboard and logs.

    Format: {Xx}{Xx}…-{agent_name}
    where each Xx is the first two letters of each ancestor tier name,
    using only the first word of hyphenated names (Nara-Narayana → Na).

    For v0 (Adi Purusha → Hamsa): returns "AdHa-{agent_name}".

    Ancestry: all tiers with tier_level <= the ticket's tier_level,
    sorted ascending by level.
    """
    tiers = tier_registry.list_active()

    ticket_level = None
    for t in tiers:
        if t.tier_name == ticket_tier_name:
            ticket_level = t.tier_level
            break

    if ticket_level is None:
        return agent_name

    ancestors = sorted(
        [t for t in tiers if t.tier_level <= ticket_level],
        key=lambda t: t.tier_level,
    )

    prefix_parts = []
    for ancestor in ancestors:
        first_word = ancestor.tier_name.split("-")[0].split()[0]
        prefix_parts.append(first_word[:2])

    prefix = "".join(prefix_parts)
    return f"{prefix}-{agent_name}"


# ---------------------------------------------------------------------------
# JSONL logging (minimal, pre-stage-13)
# ---------------------------------------------------------------------------

def _log(event_type: str, ticket_id: str | None = None, **details: Any) -> None:
    """
    Write a JSONL log entry to logs/runs.jsonl.

    Minimal implementation — stage 13 replaces this with the full
    observability.jsonl module. Uses stderr as fallback if file write fails.
    """
    import json
    from pathlib import Path

    entry = {
        "timestamp": _now(),
        "event_type": event_type,
        "ticket_id": ticket_id,
        "details": details,
    }
    try:
        log_path = Path("logs/runs.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"[scheduler] log write failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Dispatches queued tickets to worker processes.

    One Scheduler instance per server. Runs in a background thread.

    State mutated only by the run() thread:
    - _active: dict[ticket_id, (Process, start_time_float, tier_name)]
    - _running: bool

    shutdown() may be called from any thread (signal handler or server).
    """

    def __init__(
        self,
        store: TicketStore,
        tier_registry: TierRegistry,
        naming_service: NamingService,
        shutdown_grace_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._tier_registry = tier_registry
        self._naming_service = naming_service
        self._shutdown_grace = shutdown_grace_seconds
        # _active maps ticket_id → (Process, start_time, tier_name)
        self._active: dict[str, tuple[multiprocessing.Process, float, str]] = {}
        self._running = False
        self._touch_manager = TouchManager(store)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the poll loop. Blocks until shutdown() is called.

        Runs the stale ticket janitor once before the loop begins.
        """
        self._janitor()
        self._install_signal_handler()
        self._running = True
        while self._running:
            try:
                self._reap_dead()
                self._check_timeouts()
                self._dispatch_queued()
            except Exception as exc:
                print(
                    f"[scheduler] loop error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            time.sleep(_POLL_INTERVAL)

        self._graceful_shutdown()

    def shutdown(self) -> None:
        """
        Signal the poll loop to stop after the current iteration.

        Safe to call from any thread or signal handler.
        """
        self._running = False

    # ------------------------------------------------------------------
    # Janitor — runs once at startup
    # ------------------------------------------------------------------

    def _janitor(self) -> None:
        """
        Re-queue tickets left in non-terminal states by a previous crashed run.

        Scans for tickets with status in ('touched', 'in_progress'). For each,
        sets status=queued, clears touched_by and assigned_to_agent, appends
        a FailureNote with reason "orphaned by scheduler restart".

        Runs synchronously before the poll loop begins. No worker processes
        are active at this point so there is no race with _dispatch_queued().
        """
        orphan_statuses = ("touched", "in_progress")
        orphans = []
        for status in orphan_statuses:
            orphans.extend(self._store.list(status=status))

        for ticket in orphans:
            tid = ticket.envelope.id
            now = _now()

            note = FailureNote(
                ticket_id=tid,
                agent=ticket.envelope.touched_by or "unknown",
                failed_at=now,
                reason="orphaned by scheduler restart",
                raw_excerpt="",
            )

            env_dict = ticket.envelope.model_dump()
            env_dict["status"] = "queued"
            env_dict["touched_by"] = None
            env_dict["assigned_to_agent"] = None
            env_dict["updated_at"] = now
            existing_notes = env_dict.get("failure_notes", [])
            env_dict["failure_notes"] = existing_notes + [note.model_dump()]

            updated = Ticket(
                envelope=Envelope(**env_dict),
                payload=ticket.payload,
                result=ticket.result,
            )
            self._store.update(updated)

            _log("janitor_requeue", ticket_id=tid, agent=note.agent)
            print(f"[scheduler] janitor: re-queued orphaned ticket {tid}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Worker timeout checker
    # ------------------------------------------------------------------

    def _check_timeouts(self) -> None:
        """
        SIGKILL workers that have exceeded their tier's worker_timeout_seconds.

        For each timed-out worker:
        1. SIGKILL the process
        2. Call touch.forward() with a timeout reason
        3. Log to JSONL as "worker_timeout"
        4. Remove from _active

        Runs on every poll iteration after _reap_dead().
        """
        now = time.monotonic()
        timed_out = []

        for ticket_id, (proc, start_time, tier_name) in self._active.items():
            try:
                tier_config = self._tier_registry.get(tier_name)
            except KeyError:
                continue

            timeout = getattr(tier_config, "worker_timeout_seconds", 180)
            elapsed = now - start_time

            if elapsed > timeout and proc.is_alive():
                timed_out.append((ticket_id, proc, tier_name, int(elapsed)))

        for ticket_id, proc, tier_name, elapsed in timed_out:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass  # already dead

            try:
                proc.join(timeout=2.0)
            except Exception:
                pass

            try:
                self._touch_manager.forward(
                    ticket_id,
                    reason=f"worker exceeded {elapsed}s timeout",
                    raw_excerpt="",
                )
            except Exception as exc:
                print(
                    f"[scheduler] timeout forward failed for {ticket_id}: {exc}",
                    file=sys.stderr,
                )

            _log("worker_timeout", ticket_id=ticket_id, tier=tier_name, elapsed_seconds=elapsed)
            self._active.pop(ticket_id, None)

    # ------------------------------------------------------------------
    # Process reaping
    # ------------------------------------------------------------------

    def _reap_dead(self) -> None:
        """
        Remove finished processes from _active and join them.

        join(timeout=0) cleans up OS-level zombie processes on POSIX systems.
        """
        finished = [
            tid for tid, (proc, _, __) in self._active.items()
            if not proc.is_alive()
        ]
        for ticket_id in finished:
            proc, _, __ = self._active.pop(ticket_id)
            proc.join(timeout=0)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_queued(self) -> None:
        """
        Dispatch all dispatchable queued tickets.

        A ticket is dispatchable if:
        - Its tier has worker_module and worker_entrypoint configured
        - The tier's active process count is below max_parallel
        - The ticket is not already in _active

        Per-ticket dispatch errors are caught, logged, and skipped.
        The ticket stays queued and will be retried on the next poll.
        """
        queued = self._store.list(status="queued")

        # Count active processes per tier from current _active state
        active_by_tier: dict[str, int] = {}
        for _, (_, __, tier_name) in self._active.items():
            active_by_tier[tier_name] = active_by_tier.get(tier_name, 0) + 1

        for ticket in queued:
            tid = ticket.envelope.id
            if tid in self._active:
                continue

            tier_name = ticket.envelope.tier_name

            try:
                tier_config = self._tier_registry.get(tier_name)
            except KeyError:
                print(
                    f"[scheduler] unknown tier {tier_name!r} on ticket {tid} — skipping",
                    file=sys.stderr,
                )
                continue

            if not tier_config.worker_module or not tier_config.worker_entrypoint:
                continue

            current = active_by_tier.get(tier_name, 0)
            if current >= tier_config.max_parallel:
                continue

            try:
                agent_name = self._naming_service.generate(tier_name)
            except Exception as exc:
                print(
                    f"[scheduler] naming failed for tier {tier_name!r}: {exc}",
                    file=sys.stderr,
                )
                continue

            lineage = _lineage_path(self._tier_registry, tier_name, agent_name)

            try:
                self._spawn(tid, lineage, tier_config)
                active_by_tier[tier_name] = current + 1
                _log("worker_spawn", ticket_id=tid, agent=lineage, tier=tier_name)
            except Exception as exc:
                print(
                    f"[scheduler] spawn failed for ticket {tid}: {exc}",
                    file=sys.stderr,
                )
                continue

    def _spawn(
        self,
        ticket_id: str,
        lineage_path: str,
        tier_config: TierConfig,
    ) -> None:
        """
        Import the worker module, resolve the entry point, and spawn a Process.

        Passes provider_name and provider_config from tier config.
        Tracks start time for timeout enforcement.
        """
        module = importlib.import_module(tier_config.worker_module)
        entry = getattr(module, tier_config.worker_entrypoint)

        proc = multiprocessing.Process(
            target=entry,
            args=(
                ticket_id,
                lineage_path,
                self._store.db_path,
                tier_config.provider or "",
                tier_config.provider_config,
            ),
            daemon=False,
        )
        proc.start()
        self._active[ticket_id] = (proc, time.monotonic(), tier_config.tier_name)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def _install_signal_handler(self) -> None:
        """
        Install SIGINT handler to trigger graceful shutdown.

        Only installed if called from the main thread — signal handlers
        can only be set from the main thread on CPython.
        """
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._handle_sigint)

    def _handle_sigint(self, signum: int, frame: Any) -> None:
        """SIGINT handler — triggers graceful shutdown."""
        print("\n[scheduler] SIGINT received — shutting down gracefully", file=sys.stderr)
        self.shutdown()

    def _graceful_shutdown(self) -> None:
        """
        Give in-flight workers a grace period, then force-terminate.

        Sequence:
        1. Wait up to shutdown_grace_seconds for workers to finish naturally
        2. For any still running: SIGTERM, wait 5s, then SIGKILL
        3. Mark remaining tickets as killed in SQLite
        """
        if not self._active:
            return

        print(
            f"[scheduler] waiting up to {self._shutdown_grace}s for "
            f"{len(self._active)} in-flight worker(s)...",
            file=sys.stderr,
        )

        deadline = time.monotonic() + self._shutdown_grace
        while self._active and time.monotonic() < deadline:
            self._reap_dead()
            time.sleep(0.25)

        if not self._active:
            print("[scheduler] all workers finished cleanly", file=sys.stderr)
            return

        # Grace period expired — SIGTERM remaining workers
        print(
            f"[scheduler] grace period expired; terminating {len(self._active)} worker(s)",
            file=sys.stderr,
        )
        for ticket_id, (proc, _, __) in list(self._active.items()):
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        time.sleep(_SIGKILL_GRACE)

        # SIGKILL any still alive, mark tickets killed
        for ticket_id, (proc, _, __) in list(self._active.items()):
            if proc.is_alive():
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            proc.join(timeout=2.0)

            # Mark ticket as killed
            try:
                ticket = self._store.read(ticket_id)
                if ticket is not None and ticket.envelope.status not in (
                    "done", "forwarded", "aborted", "killed"
                ):
                    now = _now()
                    env_dict = ticket.envelope.model_dump()
                    env_dict["status"] = "killed"
                    env_dict["updated_at"] = now
                    updated = Ticket(
                        envelope=Envelope(**env_dict),
                        payload=ticket.payload,
                        result=ticket.result,
                    )
                    self._store.update(updated)
                    _log("worker_killed_on_shutdown", ticket_id=ticket_id)
            except Exception as exc:
                print(
                    f"[scheduler] failed to mark {ticket_id} killed: {exc}",
                    file=sys.stderr,
                )

        self._active.clear()
        print("[scheduler] shutdown complete", file=sys.stderr)