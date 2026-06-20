# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/observability/jsonl.py
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunLogger:
    """Append-only JSONL run log for MadCP.

    Thread-safe within a single process via threading.Lock. Cross-process safe
    on POSIX via O_APPEND semantics — each write opens, appends one JSON line,
    and closes, which is atomic for lines well below PIPE_BUF (~4 KB). Multiple
    worker processes writing to the same file simultaneously are safe in practice.

    Never raises from log(). Errors are printed to stderr so a logging failure
    never kills a worker or the MCP surface.

    Path convention (composition root):
        server.py reads MADHU_LOG_PATH env var; defaults to logs/runs.jsonl.
        Worker processes receive the resolved string path at spawn time.

    Schema per line:
        timestamp   ISO-8601 UTC string
        event_type  string — see EVENT_TYPES below
        ticket_id   string or null
        agent_name  string or null  (lineage path, e.g. "AdHa-vasishtha")
        details     object or null  (event-specific fields)
    """

    # Canonical event types — not enforced at runtime, documented here for
    # consistency across the codebase.
    EVENT_TYPES = frozenset({
        "worker_spawn",
        "worker_exit",
        "touch_acquire",
        "touch_release",
        "touch_forward",
        "ollama_call",
        "ollama_result",
        "mcp_submit_enter",
        "mcp_submit_exit",
    })

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # Ensure directory exists at construction time so the first log() call
        # never fails due to a missing directory.
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: str,
        *,
        ticket_id: str | None = None,
        agent_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append one JSON line. Never raises."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "ticket_id": ticket_id,
            "agent_name": agent_name,
            "details": details,
        }
        # json.dumps with default=str handles any non-serialisable detail values
        # (e.g. Path objects, enums) without crashing the logger.
        line = json.dumps(entry, default=str) + "\n"
        try:
            with self._lock:
                # Open-per-write: flush is implicit on close; O_APPEND is set by
                # the OS for mode="a", giving cross-process atomic appends.
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception as exc:  # noqa: BLE001
            print(
                f"RunLogger: failed to write {event_type!r} for ticket "
                f"{ticket_id!r}: {exc}",
                file=sys.stderr,
            )
