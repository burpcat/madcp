# server.py
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from madhu.mcp_helpers import poll_until_terminal, validate_and_build_ticket
from madhu.naming import NamingService
from madhu.scheduler import Scheduler
from madhu.store.sqlite import TicketStore
from madhu.tiers.registry import TierRegistry

# ---------------------------------------------------------------------------
# Logging — all output goes to stderr. stdout is the MCP transport and must
# never be written to by application code; doing so silently corrupts the stream.
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("madhu.server")

# ---------------------------------------------------------------------------
# Global services — None until main() initialises them.
# Tool handlers guard against None and return structured error dicts if called
# before initialisation (should not happen in normal operation).
# ---------------------------------------------------------------------------
_store: TicketStore | None = None
_scheduler: Scheduler | None = None
_scheduler_thread: threading.Thread | None = None

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp_server = FastMCP("MadCP")


@mcp_server.tool()
async def submit_ticket(envelope: dict, payload: dict) -> dict:
    """Submit a task ticket to MadCP for execution by a Hamsa-tier worker.

    REQUIRED fields in `envelope`:
      - tier_name: "Hamsa"
      - tier_level: 2  (integer)

    Server-assigned fields — do NOT supply; will be overridden if present:
      id, status, created_by_agent, created_at, updated_at,
      failure_notes, touch_history

    REQUIRED fields in `payload` for Hamsa tier:
      - type: "function_spec"
      - function_name: snake_case identifier  (e.g. "parse_query_string")
      - signature: full def line containing function_name
      - docstring: what the function does
      - constraints: list[str]  (empty list [] is valid)
      - examples: list[{"input": ..., "output": ...}]  (must be non-empty)
      - imports_allowed: list[str]

    Example:
      envelope = {"tier_name": "Hamsa", "tier_level": 2}
      payload  = {
          "type": "function_spec",
          "function_name": "reverse_string",
          "signature": "def reverse_string(s: str) -> str:",
          "docstring": "Returns the input string reversed.",
          "constraints": ["handle empty string"],
          "examples": [{"input": "hello", "output": "olleh"}],
          "imports_allowed": []
      }

    RETURNS a dict. Always check for the "error" key before treating the
    response as a resolved ticket. Possible shapes:

      Validation failure  → {"error": "...", "status": "rejected", "id": null}
      Store write error   → {"error": "...", "status": "error",    "id": null}
      Ticket disappeared  → {"error": "...", "status": "error",    "id": "<uuid>"}
      Timeout (600 s)     → full ticket dict at current state (ticket NOT killed)
      Success             → full ticket dict with embedded result

    This call BLOCKS until the ticket reaches a terminal state
    (done | failed | killed | aborted) or the 600-second timeout elapses.
    """
    if _store is None:
        return {"error": "server not yet initialised", "status": "error", "id": None}

    # Validate — sync, no I/O, safe to call from event loop thread.
    try:
        ticket = validate_and_build_ticket(envelope, payload)
    except Exception as exc:
        log.warning("submit_ticket rejected: %s", exc)
        return {"error": str(exc), "status": "rejected", "id": None}

    ticket_id = ticket.envelope.id
    log.info("submit_ticket: inserting ticket %s (tier=%s)", ticket_id, ticket.envelope.tier_name)

    # Insert — sync SQLite write; TicketStore._lock makes this thread-safe.
    # For v0 concurrency this direct call from the event loop thread is acceptable
    # (see Auditor NIT Stage 12). Wrap to return a structured error on failure.
    try:
        _store.create(ticket)
    except Exception as exc:
        log.error("submit_ticket: store.create failed for %s: %s", ticket_id, exc)
        return {"error": str(exc), "status": "error", "id": None}

    # Poll until terminal — runs in thread pool so event loop is not blocked.
    # get_running_loop() is correct here (we are inside an async def).
    loop = asyncio.get_running_loop()
    try:
        resolved = await loop.run_in_executor(
            None, poll_until_terminal, _store, ticket_id, 600.0
        )
    except RuntimeError as exc:
        log.error("submit_ticket: %s", exc)
        return {"error": str(exc), "status": "error", "id": ticket_id}

    log.info(
        "submit_ticket: ticket %s resolved → %s", ticket_id, resolved.envelope.status
    )
    # model_dump_json + json.loads ensures the return dict is fully JSON-serializable
    # regardless of how nested Pydantic models are typed on the Ticket model.
    return json.loads(resolved.model_dump_json())


@mcp_server.tool()
async def list_tickets(filter: dict | None = None) -> list[dict]:  # noqa: A002
    """List tickets in the store, optionally filtered.

    Optional `filter` dict keys (unknown keys are silently ignored):
      - status: one of queued | touched | in_progress | done |
                         failed | killed | forwarded | aborted
      - tier: tier name, e.g. "Hamsa"
      - assigned_to: agent name, e.g. "vasishtha"

    Returns an empty list if no tickets match or if an error occurs.
    """
    if _store is None:
        return []

    f = filter or {}
    try:
        tickets = _store.list(
            status=f.get("status"),
            tier=f.get("tier"),
            assigned_to=f.get("assigned_to"),
        )
        return [json.loads(t.model_dump_json()) for t in tickets]
    except Exception as exc:
        log.error("list_tickets failed: %s", exc)
        return []


@mcp_server.tool()
async def check_ticket(id: str) -> dict:  # noqa: A002
    """Return the current state of a single ticket by ID.

    Returns the full ticket dict if found.
    Returns {"error": "ticket not found", "id": "<id>"} if the ID is unknown.
    """
    if _store is None:
        return {"error": "server not yet initialised", "id": id}

    try:
        ticket = _store.read(id)
    except Exception as exc:
        log.error("check_ticket %s failed: %s", id, exc)
        return {"error": str(exc), "id": id}

    if ticket is None:
        return {"error": "ticket not found", "id": id}

    return json.loads(ticket.model_dump_json())


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def _shutdown(scheduler: Scheduler, thread: threading.Thread, timeout: float = 5.0) -> None:
    """Signal the scheduler to stop and wait up to `timeout` seconds.

    Safe to call from a POSIX signal handler — no async machinery.
    Logs outcome to stderr; calls sys.exit(0) unconditionally (signal handlers
    are not expected to return normally to the MCP stdio loop).
    """
    log.info("shutdown: signalling scheduler to stop...")
    scheduler.shutdown()
    thread.join(timeout=timeout)
    if thread.is_alive():
        log.warning(
            "shutdown: scheduler thread did not stop within %.0fs — forcing exit",
            timeout,
        )
    else:
        log.info("shutdown: scheduler stopped cleanly")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Boot MadCP: init services, start scheduler thread, install signal handlers,
    then hand off to the MCP stdio server (which runs until transport closes).

    Startup order is load-bearing: scheduler must be running before the first
    submit_ticket call can resolve, so the thread is started before mcp_server.run().
    """
    global _store, _scheduler, _scheduler_thread  # noqa: PLW0603

    project_root = Path(__file__).parent
    db_path = project_root / "data" / "palakudu.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Store ---
    log.info("initialising TicketStore at %s", db_path)
    try:
        store = TicketStore(str(db_path))
    except Exception as exc:
        log.critical("failed to initialise store: %s", exc)
        sys.exit(1)

    # --- Tier registry ---
    try:
        tier_registry = TierRegistry()
    except Exception as exc:
        log.critical("failed to load tier configs: %s", exc)
        sys.exit(1)

    # --- Naming service ---
    naming_service = NamingService(store)

    # --- Scheduler ---
    scheduler = Scheduler(store=store, tier_registry=tier_registry, naming_service=naming_service)

    # Start scheduler thread BEFORE exposing globals to tool handlers.
    # daemon=False so the scheduler gets a chance to drain _active on normal exit.
    scheduler_thread = threading.Thread(target=scheduler.run, name="madhu-scheduler", daemon=False)
    scheduler_thread.start()
    log.info("scheduler thread started (tid=%s)", scheduler_thread.ident)

    # Publish globals — tool handlers are callable from this point.
    _store = store
    _scheduler = scheduler
    _scheduler_thread = scheduler_thread

    # --- Signal handlers — must be installed from the main thread ---
    def _handle_signal(signum: int, frame: object) -> None:
        log.info("received signal %s — initiating shutdown", signal.Signals(signum).name)
        _shutdown(scheduler, scheduler_thread)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    log.info("signal handlers installed (SIGINT, SIGTERM)")

    # --- Run MCP server (blocks until stdio transport closes) ---
    log.info("MadCP ready — listening on stdio")
    mcp_server.run()


if __name__ == "__main__":
    main()
