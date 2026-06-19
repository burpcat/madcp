"""
C3 scheduler launcher.

Invoked by c3_worker_pipeline_smoke.py as a subprocess. Reads paths from
environment variables and constructs a Scheduler against them, then runs.

Env vars:
    MADHU_DB_PATH       — SQLite file path (required)
    MADHU_TICKETS_DIR   — markdown sync target dir (required)
    MADHU_LOG_PATH      — JSONL run log path (optional, scheduler may default)

If your Scheduler / TierRegistry / NamingService constructor signatures differ
from what's below, this is the file to edit — not the smoke harness itself.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Imports — adjust if your package surface differs
from madhu.store.sqlite import TicketStore
from madhu.store.markdown import MarkdownSync
from madhu.tiers.registry import TierRegistry
from madhu.naming import NamingService
from madhu.scheduler import Scheduler


def main() -> int:
    db_path = os.environ.get("MADHU_DB_PATH")
    tickets_dir = os.environ.get("MADHU_TICKETS_DIR")
    log_path = os.environ.get("MADHU_LOG_PATH")  # optional

    if not db_path or not tickets_dir:
        print(
            "FATAL: MADHU_DB_PATH and MADHU_TICKETS_DIR env vars required",
            file=sys.stderr,
        )
        return 2

    print(f"launcher: db={db_path}", file=sys.stderr)
    print(f"launcher: tickets={tickets_dir}", file=sys.stderr)
    print(f"launcher: log={log_path}", file=sys.stderr)

    # MarkdownSync — keyword first, positional fallback
    try:
        md_sync = MarkdownSync(tickets_dir=Path(tickets_dir))
    except TypeError:
        md_sync = MarkdownSync(tickets_dir)

    # TicketStore — composition-root wiring of the markdown callback
    try:
        store = TicketStore(db_path=db_path)
    except TypeError:
        store = TicketStore(db_path)
    store._on_ticket_write = md_sync.sync_ticket

    # TierRegistry — adjust constructor if it takes args other than default
    try:
        registry = TierRegistry()
    except TypeError:
        # If your registry needs a configs dir, pass it from project root
        configs_dir = Path(__file__).resolve().parent.parent / "madhu" / "tiers" / "configs"
        registry = TierRegistry(configs_dir)

    # NamingService
    naming = NamingService(store)

    # RunLogger — construct before Scheduler so it can be passed in
    run_logger = None
    if log_path:
        from madhu.observability.jsonl import RunLogger
        run_logger = RunLogger(log_path)

    # Scheduler
    try:
        scheduler = Scheduler(
            store=store,
            tier_registry=registry,
            naming_service=naming,
            run_logger=run_logger,
        )
    except TypeError as e:
        print(f"FATAL: cannot construct Scheduler — {e}", file=sys.stderr)
        return 2

    print("launcher: starting scheduler.run()", file=sys.stderr)
    scheduler.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


"""
mkdir -p /tmp/test-tickets && \
MADHU_DB_PATH=/tmp/test.db \
MADHU_TICKETS_DIR=/tmp/test-tickets \
MADHU_LOG_PATH=/tmp/test.jsonl \
python scratch/c3_scheduler_launcher.py
"""