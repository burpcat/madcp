# madhu/workers/base.py
from __future__ import annotations

"""
Base abstractions for MadCP workers.

Defines:
- Provider protocol: one method, generate(), returns raw model output
- ProviderError: raised by providers on expected failure
- BaseWorker: ABC for tier workers — owns the MTap lifecycle
- WorkerResult, WorkerFailure: return/exception types for execute()

Provider implementations live in madhu/workers/providers/.
Worker implementations (hamsa.py etc.) live alongside this file.
"""

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from madhu.store.sqlite import TicketStore
from madhu.store.touch import TouchManager


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """
    Raised by a Provider on expected failure.

    Expected failures: HTTP errors, timeouts, empty responses, connection
    refused. Unexpected failures (bugs in provider code) propagate as-is.

    Workers catch ProviderError and convert it to WorkerFailure.
    """
    pass


@runtime_checkable
class Provider(Protocol):
    """
    Protocol for LLM provider implementations.

    A Provider handles exactly one concern: sending a prompt to a model
    and returning the raw string response. No parsing, no validation,
    no ticket awareness.

    Implementations live in madhu/workers/providers/. To add a new provider:
    1. Create madhu/workers/providers/{name}.py implementing this protocol
    2. Add it to PROVIDER_REGISTRY in madhu/workers/providers/__init__.py
    3. Set provider: "{name}" in the tier YAML config

    generate() raises ProviderError on expected failure (timeout, HTTP error,
    empty response). All other exceptions propagate.
    """

    def generate(
        self,
        prompt: str,
        model: str,
        temperature: float,
        timeout: float,
    ) -> str:
        """
        Send prompt to the model and return the raw response string.

        Args:
            prompt: The full prompt string to send.
            model: Model identifier (provider-specific format).
            temperature: Sampling temperature.
            timeout: Request timeout in seconds.

        Returns:
            Raw model output as a string. Not cleaned, not validated.

        Raises:
            ProviderError: On expected failure (network, timeout, empty).
        """
        ...


# ---------------------------------------------------------------------------
# Worker result / failure types
# ---------------------------------------------------------------------------

class WorkerResult:
    """Successful result from a worker's execute() call."""

    def __init__(self, data: str, summary: str) -> None:
        self.data = data
        self.summary = summary


class WorkerFailure(Exception):
    """
    Expected worker failure — triggers a forward, not a crash.

    Use for predictable failure modes: bad model output, parse errors,
    constraint violations, provider errors. Not for unexpected exceptions
    (those propagate and are caught by BaseWorker.run()).
    """

    def __init__(self, reason: str, raw_excerpt: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw_excerpt = raw_excerpt[:500]


# ---------------------------------------------------------------------------
# Base worker
# ---------------------------------------------------------------------------

class BaseWorker(ABC):
    """
    Abstract base for MadCP workers.

    Owns the MTap lifecycle: acquire → execute → release/forward.
    Subclasses implement execute() only.

    The multiprocessing entry point in each worker module is a module-level
    run_worker(ticket_id, agent_name, db_path) function — not a method —
    so it can be pickled by multiprocessing on all platforms.
    """

    def __init__(self, ticket_id: str, agent_name: str, db_path: str, logger=None) -> None:
        self.ticket_id = ticket_id
        self.agent_name = agent_name
        self.db_path = db_path
        self._logger = logger

    def run(self) -> None:
        """
        Execute the MTap lifecycle: acquire → execute → release → write result.

        _write_result() is outside the try/except block so its failure
        cannot trigger a forward on an already-released ticket. A result-write
        failure propagates as an unhandled exception from run() — the
        scheduler detects the non-zero child process exit.
        """
        store = TicketStore(self.db_path)
        tm = TouchManager(store)

        acquired = tm.acquire(self.ticket_id, self.agent_name, logger=self._logger)
        if not acquired:
            return

        try:
            result = self.execute(store)
        except WorkerFailure as exc:
            tm.forward(self.ticket_id, self.agent_name, exc.reason, exc.raw_excerpt, logger=self._logger)
            return
        except Exception as exc:
            import traceback
            tm.forward(
                self.ticket_id,
                self.agent_name,
                f"Unexpected worker error: {type(exc).__name__}: {exc}",
                traceback.format_exc()[:500],
                logger=self._logger,
            )
            return

        # execute() succeeded — release touch, then write result.
        # These are outside the try block: _write_result() failure cannot
        # trigger a forward on an already-released (done) ticket.
        tm.release(self.ticket_id, self.agent_name, result.summary, "done", logger=self._logger)
        self._write_result(store, result)

    def _write_result(self, store: TicketStore, result: WorkerResult) -> None:
        """
        Attach the Result object to the ticket in SQLite.

        Reads the post-release ticket (status=done, touched_by=None),
        sets result, writes back. Mutates only result and updated_at —
        no Envelope reconstruction, no risk of dropping fields.
        """
        from datetime import datetime, timezone
        from madhu.schemas.envelope import Result

        ticket = store.read(self.ticket_id)
        if ticket is None:
            return

        now = datetime.now(timezone.utc).isoformat()
        ticket.result = Result(
            status="success",
            data=result.data,
            produced_at=now,
            by_agent=self.agent_name,
        )
        ticket.envelope.updated_at = now
        store.update(ticket)

    @abstractmethod
    def execute(self, store: TicketStore) -> WorkerResult:
        """
        Do the actual work for this ticket.

        Return WorkerResult on success.
        Raise WorkerFailure on expected failure.
        Raise any other exception for unexpected failures.
        """
        ...