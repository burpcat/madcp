# madhu/workers/base.py
from __future__ import annotations

"""
Base worker boilerplate for MadCP.

All tier workers inherit from BaseWorker or follow its calling convention.
The entry point for multiprocessing.Process is always a module-level function
run_worker(ticket_id, agent_name, db_path) — not a method — so it can be
pickled by multiprocessing on all platforms.

MTap contract (leaf workers):
  spawn → load → acquire → execute → release/forward → exit
No queue, no persistent state. One ticket per process lifetime.
"""

from abc import ABC, abstractmethod


class BaseWorker(ABC):
    """
    Abstract base for MadCP workers.

    Subclasses implement execute(). The run_worker() module-level function
    in each worker module is the multiprocessing entry point and calls
    this class's run() method.
    """

    def __init__(self, ticket_id: str, agent_name: str, db_path: str) -> None:
        """
        Initialise with identity and store location.

        db_path is passed from the scheduler (stage 11). Workers never
        derive the path themselves.
        """
        self.ticket_id = ticket_id
        self.agent_name = agent_name
        self.db_path = db_path

    def run(self) -> None:
        from madhu.store.sqlite import TicketStore
        from madhu.store.touch import TouchManager

        store = TicketStore(self.db_path)
        tm = TouchManager(store)

        acquired = tm.acquire(self.ticket_id, self.agent_name)
        if not acquired:
            return

        try:
            result = self.execute(store)
        except WorkerFailure as exc:
            tm.forward(self.ticket_id, exc.reason, exc.raw_excerpt)
            return
        except Exception as exc:
            import traceback
            tm.forward(
                self.ticket_id,
                f"Unexpected worker error: {type(exc).__name__}: {exc}",
                traceback.format_exc()[:500],
            )
            return

        # Write result before releasing — if this fails, the touch stays open
        # and the scheduler (stage 11) can detect a stale touch and recover.
        # A result-write failure must NOT forward — the work succeeded.
        try:
            self._write_result(store, result)
        except Exception as exc:
            import traceback
            # Log to stderr for now; stage 13 wires the JSONL logger here.
            print(
                f"[{self.agent_name}] WARNING: result write failed for {self.ticket_id}: {exc}",
                file=__import__("sys").stderr,
            )

        # Release regardless of whether result write succeeded.
        tm.release(self.ticket_id, self.agent_name, result.summary, "done")

    def _write_result(self, store, result: WorkerResult) -> None:
        """Write the result back to the ticket in SQLite."""
        from madhu.schemas.envelope import Result, Ticket, Envelope
        import json

        ticket = store.read(self.ticket_id)
        if ticket is None:
            return

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        env_dict = ticket.envelope.model_dump()
        env_dict["status"] = "done"
        env_dict["updated_at"] = now

        result_obj = Result(
            status="success",
            data=result.data,
            produced_at=now,
            by_agent=self.agent_name,
        )

        updated = Ticket(
            envelope=Envelope(**env_dict),
            payload=ticket.payload,
            result=result_obj,
        )
        store.update(updated)

    @abstractmethod
    def execute(self, store) -> WorkerResult:
        """
        Do the actual work for this ticket.

        Return a WorkerResult on success.
        Raise WorkerFailure on expected failure (bad output, parse error).
        Raise any other exception for unexpected failures — base.run() will
        forward with a traceback excerpt.
        """
        ...


class WorkerResult:
    """Successful result from a worker's execute() call."""

    def __init__(self, data: str, summary: str) -> None:
        self.data = data
        self.summary = summary


class WorkerFailure(Exception):
    """
    Expected worker failure — triggers a forward, not a crash.

    Use this for predictable failure modes: bad model output, parse errors,
    constraint violations. Not for unexpected exceptions (those propagate
    and are caught by BaseWorker.run()).
    """

    def __init__(self, reason: str, raw_excerpt: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw_excerpt = raw_excerpt[:500]  # cap excerpt length
