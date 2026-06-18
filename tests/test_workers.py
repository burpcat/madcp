# tests/test_workers.py
from __future__ import annotations

"""
Tests for madhu/workers/gemma.py and madhu/workers/base.py.

Covers:
- _strip_channel_markers(): removes <|...|> tokens
- _strip_code_fences(): removes ```python ... ``` and ``` ... ```
- _validate_single_function(): accepts valid single function, rejects zero/multi/wrong name
- _call_ollama(): success path, HTTP error, timeout, empty response (all mocked)
- GemmaWorker.execute(): success path (mocked Ollama)
- GemmaWorker.execute(): parse failure raises WorkerFailure
- GemmaWorker.execute(): wrong payload type raises WorkerFailure
- BaseWorker.run(): success path calls release with 'done'
- BaseWorker.run(): WorkerFailure calls forward
- BaseWorker.run(): acquire returns False → exits cleanly (no release/forward)

Does NOT cover:
- Stage 11 (scheduler): multiprocessing.Process spawn, MTap enforcement
- Stage 13 (JSONL log): log entries from worker events
- Real Ollama calls — all network calls are mocked
"""

import ast
import uuid
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import respx
import httpx

from madhu.workers.base import BaseWorker, WorkerFailure, WorkerResult
from madhu.workers.gemma import (
    GemmaWorker,
    _build_prompt,
    _call_ollama,
    _strip_channel_markers,
    _strip_code_fences,
    _validate_single_function,
    OLLAMA_URL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_FUNCTION = "def add_two(a: int, b: int) -> int:\n    return a + b"

VALID_PAYLOAD = {
    "type": "function_spec",
    "function_name": "add_two",
    "signature": "def add_two(a: int, b: int) -> int",
    "docstring": "Return a + b.",
    "constraints": ["handle negatives"],
    "examples": [{"input": "a=1, b=2", "output": "3"}],
    "imports_allowed": [],
}


def make_store_with_ticket(payload: dict = None) -> MagicMock:
    """Return a mock store that returns a ticket with the given payload."""
    store = MagicMock()
    ticket = MagicMock()
    ticket.payload = payload or VALID_PAYLOAD
    store.read.return_value = ticket
    return store


# ---------------------------------------------------------------------------
# _strip_channel_markers
# ---------------------------------------------------------------------------

def test_strip_channel_markers_removes_tokens():
    raw = "<|im_start|>def foo(): pass<|im_end|>"
    assert _strip_channel_markers(raw) == "def foo(): pass"


def test_strip_channel_markers_noop_on_clean():
    code = "def foo(): pass"
    assert _strip_channel_markers(code) == code


def test_strip_channel_markers_handles_empty():
    assert _strip_channel_markers("") == ""


# ---------------------------------------------------------------------------
# _strip_code_fences
# ---------------------------------------------------------------------------

def test_strip_code_fences_python_tagged():
    fenced = "```python\ndef foo(): pass\n```"
    assert _strip_code_fences(fenced) == "def foo(): pass"


def test_strip_code_fences_untagged():
    fenced = "```\ndef foo(): pass\n```"
    assert _strip_code_fences(fenced) == "def foo(): pass"


def test_strip_code_fences_noop_on_plain():
    code = "def foo(): pass"
    assert _strip_code_fences(code) == code


# ---------------------------------------------------------------------------
# _validate_single_function
# ---------------------------------------------------------------------------

def test_validate_accepts_valid_function():
    code = "def add_two(a, b):\n    return a + b"
    result = _validate_single_function(code, "add_two")
    assert result == code


def test_validate_rejects_syntax_error():
    with pytest.raises(WorkerFailure, match="AST parse failed"):
        _validate_single_function("def foo(: pass", "foo")


def test_validate_rejects_no_function():
    with pytest.raises(WorkerFailure, match="no function definition"):
        _validate_single_function("x = 1 + 2", "foo")


def test_validate_rejects_multiple_functions():
    code = "def foo(): pass\ndef bar(): pass"
    with pytest.raises(WorkerFailure, match="2 function definitions"):
        _validate_single_function(code, "foo")


def test_validate_rejects_wrong_name():
    code = "def bar(): pass"
    with pytest.raises(WorkerFailure, match="Function name mismatch"):
        _validate_single_function(code, "foo")


def test_validate_rejects_nested_only():
    """A lambda or nested-only definition has no top-level function."""
    code = "x = lambda: None"
    with pytest.raises(WorkerFailure, match="no function definition"):
        _validate_single_function(code, "x")


# ---------------------------------------------------------------------------
# _call_ollama (mocked with respx)
# ---------------------------------------------------------------------------

@respx.mock
def test_call_ollama_success():
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": "def foo(): pass"})
    )
    result = _call_ollama("write a function")
    assert result == "def foo(): pass"


@respx.mock
def test_call_ollama_http_error():
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(500, text="internal error")
    )
    with pytest.raises(WorkerFailure, match="HTTP error"):
        _call_ollama("write a function")


@respx.mock
def test_call_ollama_empty_response():
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": ""})
    )
    with pytest.raises(WorkerFailure, match="empty response"):
        _call_ollama("write a function")


def test_call_ollama_timeout():
    with respx.mock:
        respx.post(OLLAMA_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(WorkerFailure, match="timed out"):
            _call_ollama("write a function")


def test_call_ollama_connection_error():
    with respx.mock:
        respx.post(OLLAMA_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(WorkerFailure, match="connection error"):
            _call_ollama("write a function")


# ---------------------------------------------------------------------------
# GemmaWorker.execute() — mocked store + Ollama
# ---------------------------------------------------------------------------

@respx.mock
def test_gemma_execute_success():
    """execute() returns WorkerResult with the cleaned function code."""
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": VALID_FUNCTION})
    )
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = GemmaWorker(ticket_id="t-001", agent_name="vasishtha", db_path=":memory:")
    result = worker.execute(store)
    assert isinstance(result, WorkerResult)
    assert "add_two" in result.data
    assert "add_two" in result.summary


@respx.mock
def test_gemma_execute_strips_fences():
    """execute() strips code fences before validation."""
    fenced = f"```python\n{VALID_FUNCTION}\n```"
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": fenced})
    )
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = GemmaWorker(ticket_id="t-002", agent_name="vasishtha", db_path=":memory:")
    result = worker.execute(store)
    assert "```" not in result.data


@respx.mock
def test_gemma_execute_parse_failure_raises_worker_failure():
    """execute() raises WorkerFailure when Gemma returns multiple functions."""
    multi = "def add_two(a, b): return a+b\ndef subtract(a, b): return a-b"
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": multi})
    )
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = GemmaWorker(ticket_id="t-003", agent_name="vasishtha", db_path=":memory:")
    with pytest.raises(WorkerFailure, match="2 function definitions"):
        worker.execute(store)


def test_gemma_execute_wrong_payload_type():
    """execute() raises WorkerFailure for non-function_spec payloads."""
    bad_payload = {"type": "task_brief", "goal": "something"}
    store = make_store_with_ticket(bad_payload)
    worker = GemmaWorker(ticket_id="t-004", agent_name="vasishtha", db_path=":memory:")
    with pytest.raises(WorkerFailure, match="Invalid function_spec payload"):
        worker.execute(store)


def test_gemma_execute_missing_ticket():
    """execute() raises WorkerFailure if the ticket is not in the store."""
    store = MagicMock()
    store.read.return_value = None
    worker = GemmaWorker(ticket_id="t-005", agent_name="vasishtha", db_path=":memory:")
    with pytest.raises(WorkerFailure, match="not found in store"):
        worker.execute(store)


# ---------------------------------------------------------------------------
# BaseWorker.run() lifecycle
# ---------------------------------------------------------------------------

def test_base_run_success_calls_release():
    """
    run() calls tm.release() with 'done' on successful execute().
    Uses a concrete subclass with a hardcoded execute() return.
    """
    class SuccessWorker(BaseWorker):
        def execute(self, store):
            return WorkerResult(data="def foo(): pass", summary="wrote the function")

    store = MagicMock()
    tm = MagicMock()
    store.read.return_value = None
    tm.acquire.return_value = True

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = SuccessWorker("t-ok", "vasishtha", ":memory:")
        worker.run()

    tm.release.assert_called_once_with("t-ok", "vasishtha", "wrote the function", "done")
    tm.forward.assert_not_called()


def test_base_run_worker_failure_calls_forward():
    """run() calls tm.forward() when execute() raises WorkerFailure."""
    class FailWorker(BaseWorker):
        def execute(self, store):
            raise WorkerFailure("bad output", "raw junk")

    store = MagicMock()
    tm = MagicMock()
    tm.acquire.return_value = True

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = FailWorker("t-fail", "vasishtha", ":memory:")
        worker.run()

    tm.forward.assert_called_once_with("t-fail", "bad output", "raw junk")
    tm.release.assert_not_called()


def test_base_run_acquire_false_exits_cleanly():
    """run() exits without release or forward if acquire() returns False."""
    class AnyWorker(BaseWorker):
        def execute(self, store):
            return WorkerResult(data="def foo(): pass", summary="wrote the function")

    store = MagicMock()
    tm = MagicMock()
    tm.acquire.return_value = False

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = AnyWorker("t-miss", "vasishtha", ":memory:")
        worker.run()

    tm.release.assert_not_called()
    tm.forward.assert_not_called()

@respx.mock
def test_gemma_execute_with_real_store():
    """
    execute() works with a real TicketStore that returns a FunctionSpec on read.
    Catches mock-vs-real divergence on payload type.
    """
    from madhu.store.sqlite import TicketStore
    from madhu.schemas.envelope import Envelope, Ticket
    from madhu.schemas.payloads import FunctionSpec

    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"response": VALID_FUNCTION})
    )

    store = TicketStore(":memory:")
    spec = FunctionSpec(
        function_name="add_two",
        signature="def add_two(a: int, b: int) -> int",
        docstring="Return a + b.",
        constraints=["handle negatives"],
        examples=[{"input": "a=1, b=2", "output": "3"}],
        imports_allowed=[],
    )
    ticket = Ticket(
        envelope=Envelope(
            id="real-store-001",
            tier_name="Hamsa",
            tier_level=2,
            status="queued",
            created_by_agent="param-aatma",
        ),
        payload=spec.model_dump(),
    )
    store.create(ticket)

    worker = GemmaWorker(ticket_id="real-store-001", agent_name="vasishtha", db_path=":memory:")
    result = worker.execute(store)
    assert "add_two" in result.data
