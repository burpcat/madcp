# madhu/mcp_helpers.py
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

from madhu.schemas.envelope import Envelope, Ticket
from madhu.schemas.payloads import FunctionSpec
from madhu.store.sqlite import TicketStore
from madhu.schemas.payloads import FunctionSpec


log = logging.getLogger(__name__)

TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "killed", "aborted"})

# Dispatch table — extend here when new payload types are added.
_PAYLOAD_VALIDATORS: dict[str, type] = {
    "function_spec": FunctionSpec,
}


def validate_and_build_ticket(envelope: dict, payload: dict) -> Ticket:
    """Validate caller-supplied dicts and return a Ticket ready for insertion.

    Server-side fields are always overridden regardless of caller input:
    id, status, created_by_agent, created_at, updated_at, failure_notes, touch_history.
    The caller cannot supply a meaningful value for any of these.

    Called synchronously from the submit_ticket tool handler (which dispatches it
    via run_in_executor so blocking here is safe).

    Raises:
        ValueError: if payload["type"] is missing or unsupported.
        pydantic.ValidationError: if envelope or payload fails schema validation.
    """
    # Resolve payload type first — cheap, fails fast before any Pydantic work.
    payload_type = payload.get("type", "")
    if payload_type not in _PAYLOAD_VALIDATORS:
        raise ValueError(
            f"unsupported payload type: {payload_type!r}. "
            f"Supported types: {sorted(_PAYLOAD_VALIDATORS)}"
        )

    # Inject server-side fields — copy first so we never mutate caller's dict.
    now = datetime.now(timezone.utc).isoformat()
    env = dict(envelope)
    env["id"] = str(uuid.uuid4())          # always fresh — prevents stale-ID reuse
    env["status"] = "queued"               # server sets initial status
    env["created_by_agent"] = "param-aatma"  # opaque; external identity is always param-aatma
    env["created_at"] = now
    env["updated_at"] = now
    env["failure_notes"] = []
    env["touch_history"] = []
    env.setdefault("schema_version", "1.0")
    env.setdefault("mtap", True)
    env.setdefault("collaboration_mode", "solo")

    validated_envelope = Envelope.model_validate(env)

    pay = dict(payload)
    pay.setdefault("schema_version", "1.0")
    validated_payload = _PAYLOAD_VALIDATORS[payload_type].model_validate(pay)

    return Ticket(envelope=validated_envelope, payload=validated_payload.model_dump(), result=None)
    # return Ticket(envelope=validated_envelope, payload=validated_payload, result=None)


def poll_until_terminal(
    store: TicketStore,
    ticket_id: str,
    timeout: float = 600.0,
) -> Ticket:
    """Block until the ticket reaches a terminal state or timeout elapses.

    Synchronous by design — intended to run in a thread pool executor (via
    asyncio.get_running_loop().run_in_executor) so time.sleep does not block
    the event loop. store.read() acquires TicketStore._lock; safe from any thread.

    On timeout: logs a warning and returns the current ticket state. The ticket
    is NOT killed — it remains in the store and the worker will still complete it.

    Raises:
        RuntimeError: if the ticket disappears from the store mid-poll.
    """
    deadline = time.monotonic() + timeout
    poll_interval = 0.5  # matches scheduler poll cadence

    while True:
        ticket = store.read(ticket_id)
        if ticket is None:
            raise RuntimeError(
                f"ticket {ticket_id!r} disappeared from store during polling"
            )

        if ticket.envelope.status in TERMINAL_STATUSES:
            return ticket

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "submit_ticket: timeout after %.0fs — ticket %s still %s; "
                "returning current state without killing",
                timeout,
                ticket_id,
                ticket.envelope.status,
            )
            return ticket

        time.sleep(min(poll_interval, remaining))
