# madhu/scheduler.py
from __future__ import annotations

"""
Scheduler for MadCP.

Polls SQLite every 0.5s for queued tickets and dispatches workers as
multiprocessing.Process instances. Enforces max_parallel per tier.
Generates agent lineage paths (AdHa-vasishtha style) at spawn time.

MTap invariant: each queued ticket gets its own fresh process.
Processes are never reused across tickets.

Called by server.py (stage 12) in a background thread:
    scheduler = Scheduler(store, tier_registry, naming_service)
    thread = threading.Thread(target=scheduler.run, daemon=True)
    thread.start()

Shutdown: call scheduler.shutdown() to signal the loop to stop.
The loop exits after the current iteration completes — in-flight workers
are not killed.

Pool exhaustion behavior: if naming_service.generate() raises (pool
exhausted), the ticket stays queued and dispatch is retried on the next
poll. This produces a log entry every 0.5s until a slot opens — expected
behavior, not a bug.
"""

import importlib
import multiprocessing
import sys
import threading
import time
from typing import Any

from madhu.naming import NamingService
from madhu.store.sqlite import TicketStore
from madhu.tiers.registry import TierConfig, TierRegistry

_POLL_INTERVAL = 0.5  # seconds — locked, not configurable in v0


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

    Ancestry is defined as all tiers with tier_level <= the ticket's tier_level,
    sorted ascending by level.

    Args:
        tier_registry: loaded TierRegistry instance
        ticket_tier_name: the tier_name on the ticket's envelope
        agent_name: the assigned agent name (already lowercased if leaf tier)

    Returns:
        Full lineage path string, e.g. "AdHa-vasishtha"
    """
    tiers = tier_registry.list_active()

    # Find the tier_level of the ticket's tier
    ticket_level = None
    for t in tiers:
        if t.tier_name == ticket_tier_name:
            ticket_level = t.tier_level
            break

    if ticket_level is None:
        # Defensive: unknown tier — return agent name without prefix
        return agent_name

    # Ancestors: all tiers at or above this ticket's level, sorted ascending
    ancestors = [t for t in tiers if t.tier_level <= ticket_level]
    ancestors.sort(key=lambda t: t.tier_level)

    # Build prefix: first two letters of first word of each ancestor tier name
    prefix_parts = []
    for ancestor in ancestors:
        first_word = ancestor.tier_name.split("-")[0].split()[0]
        prefix_parts.append(first_word[:2])

    prefix = "".join(prefix_parts)
    return f"{prefix}-{agent_name}"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Dispatches queued tickets to worker processes.

    One Scheduler instance per server. Runs in a background thread.
    All dispatch state (_active, _running) is only mutated by the run()
    thread — no locking needed on _active.
    """

    def __init__(
        self,
        store: TicketStore,
        tier_registry: TierRegistry,
        naming_service: NamingService,
    ) -> None:
        """
        Initialise with injected dependencies.

        Does not start the poll loop — call run() to start.
        """
        self._store = store
        self._tier_registry = tier_registry
        self._naming_service = naming_service
        self._active: dict[str, multiprocessing.Process] = {}
        self._running = False

    def run(self) -> None:
        """
        Start the poll loop. Blocks until shutdown() is called.

        Polls every 0.5s. On each iteration:
        1. Reap finished processes
        2. List queued tickets
        3. For each queued ticket: dispatch if tier has capacity
        """
        self._running = True
        while self._running:
            try:
                self._reap_dead()
                self._dispatch_queued()
            except Exception as exc:
                # Loop-level errors (e.g. SQLite gone) are logged but don't
                # crash the scheduler. Per-ticket errors are caught inside
                # _dispatch_queued().
                print(
                    f"[scheduler] loop error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            time.sleep(_POLL_INTERVAL)

    def shutdown(self) -> None:
        """
        Signal the poll loop to stop after the current iteration.

        Does not kill in-flight workers. Safe to call from any thread.
        """
        self._running = False

    def _reap_dead(self) -> None:
        """
        Remove finished processes from _active and join them.

        join(timeout=0) cleans up OS-level zombie processes on POSIX systems.
        Only joins processes that are confirmed not alive — never blocks.
        """
        finished = [
            ticket_id
            for ticket_id, proc in self._active.items()
            if not proc.is_alive()
        ]
        for ticket_id in finished:
            proc = self._active.pop(ticket_id)
            proc.join(timeout=0)  # clean up zombie; non-blocking

    def _dispatch_queued(self) -> None:
        """
        Dispatch all dispatchable queued tickets.

        A ticket is dispatchable if:
        - Its tier has a worker_module and worker_entrypoint configured
        - The tier's active process count is below max_parallel
        - The ticket is not already in _active

        Per-ticket dispatch errors are caught, logged, and skipped.
        The ticket stays queued and will be retried on the next poll.
        """
        queued = self._store.list(status="queued")

        # Count active processes per tier
        active_by_tier: dict[str, int] = {}
        for ticket_id in self._active:
            # Look up which tier this ticket belongs to
            ticket = self._store.read(ticket_id)
            if ticket is not None:
                tier_name = ticket.envelope.tier_name
                active_by_tier[tier_name] = active_by_tier.get(tier_name, 0) + 1

        for ticket in queued:
            if ticket.envelope.id in self._active:
                continue  # already dispatched

            tier_name = ticket.envelope.tier_name

            try:
                tier_config = self._tier_registry.get(tier_name)
            except KeyError:
                print(
                    f"[scheduler] unknown tier {tier_name!r} on ticket "
                    f"{ticket.envelope.id} — skipping",
                    file=sys.stderr,
                )
                continue

            # Skip tiers with no worker configured (e.g. Adi Purusha)
            if not tier_config.worker_module or not tier_config.worker_entrypoint:
                continue

            # Check capacity
            current = active_by_tier.get(tier_name, 0)
            if current >= tier_config.max_parallel:
                continue

            # Assign agent name
            try:
                agent_name = self._naming_service.generate(tier_name)
            except Exception as exc:
                # Pool exhausted or naming error — ticket stays queued
                print(
                    f"[scheduler] naming failed for tier {tier_name!r}: {exc}",
                    file=sys.stderr,
                )
                continue

            # Build lineage path
            lineage = _lineage_path(
                self._tier_registry,
                tier_name,
                agent_name,
            )

            # Spawn worker process
            try:
                self._spawn(ticket.envelope.id, lineage, tier_config)
                active_by_tier[tier_name] = current + 1
            except Exception as exc:
                print(
                    f"[scheduler] spawn failed for ticket {ticket.envelope.id}: {exc}",
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

        Passes provider_name and provider_config from tier config into
        run_worker() — closing the Stage 9 wiring gap.

        The spawned process is tracked in self._active by ticket_id.
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
            daemon=False,  # workers must not be killed when parent exits mid-work
        )
        proc.start()
        self._active[ticket_id] = proc
