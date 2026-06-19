# tests/test_workers.py
from __future__ import annotations

"""
Tests for madhu/workers/ — base, hamsa, providers.

Covers:
- Provider protocol: OllamaProvider.generate() success + failure modes (mocked httpx)
- PROVIDER_REGISTRY: contains exactly {"ollama": OllamaProvider} at v0
- Unknown provider name raises WorkerFailure
- _strip_channel_markers, _strip_code_fences, _validate_single_function
- HamsaWorker.execute(): success path with stub provider (not mocked httpx)
- HamsaWorker.execute(): ProviderError → WorkerFailure
- HamsaWorker.execute(): parse failure raises WorkerFailure
- HamsaWorker.execute(): wrong payload type raises WorkerFailure
- HamsaWorker.execute(): with real TicketStore (catches mock-vs-real gaps)
- BaseWorker.run(): success path calls release with 'done'
- BaseWorker.run(): WorkerFailure calls forward
- BaseWorker.run(): acquire returns False → exits cleanly

Does NOT cover:
- Stage 10 (tier registry): provider_name/config read from YAML
- Stage 11 (scheduler): multiprocessing.Process spawn
- Stage 13 (JSONL log): log entries from worker events
- Real Ollama calls — all network calls are mocked
"""

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from madhu.workers.base import BaseWorker, ProviderError, WorkerFailure, WorkerResult
from madhu.workers.hamsa import (
    HamsaWorker,
    _build_prompt,
    _strip_channel_markers,
    _strip_code_fences,
    _validate_single_function,
)
from madhu.workers.providers import PROVIDER_REGISTRY
from madhu.workers.providers.ollama import OllamaProvider


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

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"


class StubProvider:
    """
    Test double for the Provider protocol.
    Returns a hardcoded response without any network calls.
    """
    def __init__(self, response: str = VALID_FUNCTION, raises: Exception = None):
        self._response = response
        self._raises = raises

    def generate(self, prompt: str, model: str, temperature: float, timeout: float) -> str:
        if self._raises:
            raise self._raises
        return self._response


def make_hamsa_worker(
    ticket_id: str = "t-001",
    agent_name: str = "vasishtha",
    provider: object = None,
) -> HamsaWorker:
    """Return a HamsaWorker with a stub provider injected."""
    worker = HamsaWorker(
        ticket_id=ticket_id,
        agent_name=agent_name,
        db_path=":memory:",
        provider_name="ollama",
    )
    if provider is not None:
        # Inject stub by patching _make_provider
        worker._make_provider = lambda: provider
    return worker


def make_store_with_ticket(payload: dict = None) -> MagicMock:
    """Return a mock store returning a ticket with the given payload."""
    store = MagicMock()
    store.read.return_value = None  # safe default; override per test
    ticket = MagicMock()
    ticket.payload = payload or VALID_PAYLOAD
    store.read.return_value = ticket
    return store


# ---------------------------------------------------------------------------
# PROVIDER_REGISTRY
# ---------------------------------------------------------------------------

def test_registry_contains_ollama():
    assert "ollama" in PROVIDER_REGISTRY
    assert PROVIDER_REGISTRY["ollama"] is OllamaProvider


def test_registry_has_exactly_one_entry_at_v0():
    """Catches accidental additions to the registry."""
    assert set(PROVIDER_REGISTRY.keys()) == {"ollama"}


# ---------------------------------------------------------------------------
# OllamaProvider — mocked httpx
# ---------------------------------------------------------------------------

@respx.mock
def test_ollama_provider_success():
    respx.post(OLLAMA_GENERATE_URL).mock(
        return_value=httpx.Response(200, json={"response": "def foo(): pass"})
    )
    provider = OllamaProvider()
    result = provider.generate("write a function", "model-x", 0.2, 30.0)
    assert result == "def foo(): pass"


@respx.mock
def test_ollama_provider_http_error():
    respx.post(OLLAMA_GENERATE_URL).mock(
        return_value=httpx.Response(500, text="internal error")
    )
    provider = OllamaProvider()
    with pytest.raises(ProviderError, match="HTTP error"):
        provider.generate("prompt", "model", 0.2, 30.0)


@respx.mock
def test_ollama_provider_empty_response():
    respx.post(OLLAMA_GENERATE_URL).mock(
        return_value=httpx.Response(200, json={"response": ""})
    )
    provider = OllamaProvider()
    with pytest.raises(ProviderError, match="empty response"):
        provider.generate("prompt", "model", 0.2, 30.0)


def test_ollama_provider_timeout():
    with respx.mock:
        respx.post(OLLAMA_GENERATE_URL).mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        provider = OllamaProvider()
        with pytest.raises(ProviderError, match="timed out"):
            provider.generate("prompt", "model", 0.2, 30.0)


def test_ollama_provider_connection_error():
    with respx.mock:
        respx.post(OLLAMA_GENERATE_URL).mock(
            side_effect=httpx.ConnectError("refused")
        )
        provider = OllamaProvider()
        with pytest.raises(ProviderError, match="connection error"):
            provider.generate("prompt", "model", 0.2, 30.0)


def test_ollama_provider_custom_endpoint():
    """OllamaProvider uses the configured endpoint, not hardcoded localhost."""
    provider = OllamaProvider(endpoint="http://myserver:11434")
    assert provider._generate_url == "http://myserver:11434/api/generate"


# ---------------------------------------------------------------------------
# _strip_channel_markers
# ---------------------------------------------------------------------------

def test_strip_channel_markers_removes_tokens():
    raw = "<|im_start|>def foo(): pass<|im_end|>"
    assert _strip_channel_markers(raw) == "def foo(): pass"


def test_strip_channel_markers_noop_on_clean():
    code = "def foo(): pass"
    assert _strip_channel_markers(code) == code


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
    assert _strip_code_fences("def foo(): pass") == "def foo(): pass"


# ---------------------------------------------------------------------------
# _validate_single_function
# ---------------------------------------------------------------------------

def test_validate_accepts_valid_function():
    code = "def add_two(a, b):\n    return a + b"
    assert _validate_single_function(code, "add_two") == code


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
    with pytest.raises(WorkerFailure, match="Function name mismatch"):
        _validate_single_function("def bar(): pass", "foo")


def test_validate_rejects_nested_only():
    """A lambda has no top-level function definition."""
    with pytest.raises(WorkerFailure, match="no function definition"):
        _validate_single_function("x = lambda: None", "x")


# ---------------------------------------------------------------------------
# HamsaWorker.execute() — stub provider injected
# ---------------------------------------------------------------------------

def test_hamsa_execute_success_with_stub():
    """execute() returns WorkerResult when stub provider returns valid code."""
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = make_hamsa_worker(provider=StubProvider(response=VALID_FUNCTION))
    result = worker.execute(store)
    assert isinstance(result, WorkerResult)
    assert "add_two" in result.data
    assert "add_two" in result.summary


def test_hamsa_execute_provider_error_raises_worker_failure():
    """ProviderError from provider is converted to WorkerFailure."""
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = make_hamsa_worker(
        provider=StubProvider(raises=ProviderError("timed out"))
    )
    with pytest.raises(WorkerFailure, match="timed out"):
        worker.execute(store)


def test_hamsa_execute_strips_fences():
    """execute() strips code fences before validation."""
    fenced = f"```python\n{VALID_FUNCTION}\n```"
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = make_hamsa_worker(provider=StubProvider(response=fenced))
    result = worker.execute(store)
    assert "```" not in result.data


def test_hamsa_execute_parse_failure_raises_worker_failure():
    """execute() raises WorkerFailure when provider returns multiple functions."""
    multi = "def add_two(a, b): return a+b\ndef subtract(a, b): return a-b"
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = make_hamsa_worker(provider=StubProvider(response=multi))
    with pytest.raises(WorkerFailure, match="2 function definitions"):
        worker.execute(store)


def test_hamsa_execute_wrong_payload_type():
    """execute() raises WorkerFailure for non-function_spec payloads."""
    bad_payload = {"type": "task_brief", "goal": "something"}
    store = make_store_with_ticket(bad_payload)
    worker = make_hamsa_worker(provider=StubProvider())
    with pytest.raises(WorkerFailure, match="Invalid function_spec payload"):
        worker.execute(store)


def test_hamsa_execute_missing_ticket():
    """execute() raises WorkerFailure if ticket not in store."""
    store = MagicMock()
    store.read.return_value = None
    worker = make_hamsa_worker(provider=StubProvider())
    with pytest.raises(WorkerFailure, match="not found in store"):
        worker.execute(store)


def test_hamsa_execute_unknown_provider():
    """Unknown provider name raises WorkerFailure with clear message."""
    store = make_store_with_ticket(VALID_PAYLOAD)
    worker = HamsaWorker("t-x", "vasishtha", ":memory:", provider_name="vllm")
    with pytest.raises(WorkerFailure, match="Unknown provider"):
        worker.execute(store)


# ---------------------------------------------------------------------------
# HamsaWorker.execute() — real TicketStore integration
# ---------------------------------------------------------------------------

def test_hamsa_execute_with_real_store():
    """
    execute() works with a real TicketStore returning a FunctionSpec on read.
    Catches mock-vs-real divergence on payload type.
    """
    from madhu.schemas.envelope import Envelope, Ticket
    from madhu.schemas.payloads import FunctionSpec
    from madhu.store.sqlite import TicketStore

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

    worker = make_hamsa_worker(
        ticket_id="real-store-001",
        provider=StubProvider(response=VALID_FUNCTION),
    )
    result = worker.execute(store)
    assert "add_two" in result.data


# ---------------------------------------------------------------------------
# BaseWorker.run() lifecycle
# ---------------------------------------------------------------------------

def test_base_run_success_calls_release():
    """run() calls tm.release() with 'done' on successful execute()."""
    class SuccessWorker(BaseWorker):
        def execute(self, store):
            return WorkerResult(data="def foo(): pass", summary="wrote the function")

    store = MagicMock()
    store.read.return_value = None  # _write_result exits early — not under test here
    tm = MagicMock()
    tm.acquire.return_value = True

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = SuccessWorker("t-ok", "vasishtha", ":memory:")
        worker.run()

    tm.release.assert_called_once_with("t-ok", "vasishtha", "wrote the function", "done", logger=None)
    tm.forward.assert_not_called()


def test_base_run_worker_failure_calls_forward():
    """run() calls tm.forward() when execute() raises WorkerFailure."""
    class FailWorker(BaseWorker):
        def execute(self, store):
            raise WorkerFailure("bad output", "raw junk")

    store = MagicMock()
    store.read.return_value = None
    tm = MagicMock()
    tm.acquire.return_value = True

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = FailWorker("t-fail", "vasishtha", ":memory:")
        worker.run()

    tm.forward.assert_called_once_with("t-fail", "vasishtha", "bad output", "raw junk", logger=None)
    tm.release.assert_not_called()


def test_base_run_acquire_false_exits_cleanly():
    """run() exits without release or forward if acquire() returns False."""
    class AnyWorker(BaseWorker):
        def execute(self, store):
            return WorkerResult(data="x", summary="x")

    store = MagicMock()
    store.read.return_value = None
    tm = MagicMock()
    tm.acquire.return_value = False

    with patch("madhu.workers.base.TicketStore", return_value=store), \
         patch("madhu.workers.base.TouchManager", return_value=tm):
        worker = AnyWorker("t-miss", "vasishtha", ":memory:")
        worker.run()

    tm.release.assert_not_called()
    tm.forward.assert_not_called()