# madhu/workers/hamsa.py
from __future__ import annotations

"""
Hamsa-tier worker for MadCP.

Handles function_spec payloads. Reads provider config from the tier registry
(stage 10), instantiates the provider via PROVIDER_REGISTRY, calls
provider.generate() for raw output, then validates and writes the result.

No salvage from alpha — built from scratch per stage 9 spec.

Entry point for multiprocessing.Process:
    run_worker(ticket_id, agent_name, db_path)
"""

import ast
import re

from madhu.schemas.payloads import FunctionSpec
from madhu.store.sqlite import TicketStore
from madhu.workers.base import BaseWorker, ProviderError, WorkerFailure, WorkerResult
from madhu.workers.providers import PROVIDER_REGISTRY


# ---------------------------------------------------------------------------
# Default provider config (used until stage 10 wires tier registry)
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER = "ollama"
_DEFAULT_MODEL = "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0"
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_ENDPOINT = "http://localhost:11434"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(payload: FunctionSpec) -> str:
    """
    Build the Ollama prompt from a FunctionSpec instance.

    Instructs the model to output exactly one Python function — no prose,
    no markdown fences, no explanations.
    """
    constraint_block = (
        "\n".join(f"- {c}" for c in payload.constraints)
        if payload.constraints else "none"
    )
    example_block = (
        "\n".join(
            f"  input: {ex.input}  →  output: {ex.output}"
            for ex in payload.examples
        )
        if payload.examples else "  (none)"
    )
    imports_block = (
        ", ".join(payload.imports_allowed)
        if payload.imports_allowed else "none"
    )

    return f"""You are a Python function generator. Output ONLY a single Python function definition.
Do not include any prose, explanation, markdown code fences, or comments outside the function.
Do not define more than one function.
The function must match this signature exactly: {payload.signature}

Function name: {payload.function_name}
Docstring: {payload.docstring}

Constraints:
{constraint_block}

Examples:
{example_block}

Allowed imports (only these, at the top of the function body if needed): {imports_block}

Output the function definition now:"""


# ---------------------------------------------------------------------------
# Response cleaning
# ---------------------------------------------------------------------------

def _strip_channel_markers(text: str) -> str:
    """Remove <|channel|> style markers that some models emit."""
    return re.sub(r"<\|[^|]*\|>", "", text).strip()


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences if the model wraps its output."""
    text = re.sub(r"^```(?:python)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


# ---------------------------------------------------------------------------
# AST validation
# ---------------------------------------------------------------------------

def _validate_single_function(code: str, expected_name: str) -> str:
    """
    AST-parse code and confirm exactly one top-level function with
    the expected name. Returns cleaned code on success.

    Raises WorkerFailure if:
    - code is not valid Python
    - zero or more than one top-level function definition
    - function name does not match expected_name
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise WorkerFailure(
            reason=f"AST parse failed: {exc}",
            raw_excerpt=code[:500],
        )

    func_defs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.col_offset == 0
    ]

    if len(func_defs) == 0:
        raise WorkerFailure(
            reason="Gemma returned no function definition",
            raw_excerpt=code[:500],
        )

    if len(func_defs) > 1:
        raise WorkerFailure(
            reason=f"Gemma returned {len(func_defs)} function definitions (expected 1)",
            raw_excerpt=code[:500],
        )

    actual_name = func_defs[0].name
    if actual_name != expected_name:
        raise WorkerFailure(
            reason=f"Function name mismatch: expected {expected_name!r}, got {actual_name!r}",
            raw_excerpt=code[:500],
        )

    return code


# ---------------------------------------------------------------------------
# Worker class
# ---------------------------------------------------------------------------

class HamsaWorker(BaseWorker):
    """
    Hamsa-tier worker. Calls an LLM provider to implement a function_spec.

    Provider is selected via PROVIDER_REGISTRY using the tier config's
    provider name (stage 10). Until stage 10 wires the tier registry,
    defaults to OllamaProvider with hardcoded defaults.

    MTap: spawned fresh per ticket, exits after one execution.
    """

    def __init__(
        self,
        ticket_id: str,
        agent_name: str,
        db_path: str,
        provider_name: str = _DEFAULT_PROVIDER,
        provider_config: dict | None = None,
        logger=None,
    ) -> None:
        super().__init__(ticket_id, agent_name, db_path)
        self._provider_name = provider_name
        self._provider_config = provider_config or {}
    
    def _make_provider(self):
        """
        Instantiate the provider from PROVIDER_REGISTRY.

        Raises KeyError (wrapped as WorkerFailure by execute()) if
        provider_name is not registered.
        """
        if self._provider_name not in PROVIDER_REGISTRY:
            raise WorkerFailure(
                reason=f"Unknown provider {self._provider_name!r}. "
                    f"Registered: {list(PROVIDER_REGISTRY.keys())}",
                raw_excerpt="",
            )
        cls = PROVIDER_REGISTRY[self._provider_name]
        # provider_config is split into two concerns:
        # - constructor kwargs: provider init config (e.g. endpoint, api_key)
        # - per-call kwargs: model, temperature, timeout — consumed via .get()
        #   in execute() and passed to provider.generate(), NOT to __init__()
        # For OllamaProvider: only 'endpoint' goes to __init__(); everything else
        # is per-call. Keep this split explicit when adding new providers.
        constructor_kwargs = {
            k: v for k, v in self._provider_config.items()
            if k not in ("model", "temperature", "timeout")
        }
        return cls(**constructor_kwargs)

    def execute(self, store: TicketStore) -> WorkerResult:
        """
        Read ticket, call provider, validate output, return result.

        Raises WorkerFailure for all expected failure modes.
        """
        ticket = store.read(self.ticket_id)
        if ticket is None:
            raise WorkerFailure(
                reason=f"Ticket {self.ticket_id!r} not found in store",
                raw_excerpt="",
            )

        payload = ticket.payload

        # Normalise to FunctionSpec — store returns dict on read
        if isinstance(payload, dict):
            try:
                payload = FunctionSpec(**payload)
            except Exception as exc:
                raise WorkerFailure(
                    reason=f"Invalid function_spec payload: {exc}",
                    raw_excerpt=str(payload)[:500],
                )

        if not isinstance(payload, FunctionSpec):
            raise WorkerFailure(
                reason=f"HamsaWorker only handles FunctionSpec payloads, "
                       f"got {type(payload).__name__}",
                raw_excerpt="",
            )

        provider = self._make_provider()
        prompt = _build_prompt(payload)

        # Call provider — raises ProviderError on network/HTTP failure
        try:
            raw_response = provider.generate(
                prompt=prompt,
                model=self._provider_config.get("model", _DEFAULT_MODEL),
                temperature=self._provider_config.get("temperature", _DEFAULT_TEMPERATURE),
                timeout=self._provider_config.get("timeout", _DEFAULT_TIMEOUT),
            )
        except ProviderError as exc:
            raise WorkerFailure(reason=str(exc), raw_excerpt="")

        # Clean and validate
        cleaned = _strip_channel_markers(raw_response)
        cleaned = _strip_code_fences(cleaned)
        code = _validate_single_function(cleaned, payload.function_name)

        return WorkerResult(
            data=code,
            summary=f"implemented {payload.function_name}() — {len(code)} chars",
        )


# ---------------------------------------------------------------------------
# Multiprocessing entry point
# ---------------------------------------------------------------------------

def run_worker(
    ticket_id: str,
    agent_name: str,
    db_path: str,
    provider_name: str = _DEFAULT_PROVIDER,
    provider_config: dict | None = None,
    log_path: str | None = None,
) -> None:
    """
    Entry point for multiprocessing.Process.

    Module-level function (not a method) — required for pickling on macOS
    (spawn context). Provider name and config now passed from the scheduler
    via TierRegistry — closes the Stage 9 wiring gap noted in the builder report.

    Args:
        ticket_id: UUID of the ticket to work
        agent_name: lineage path assigned by the scheduler (e.g. AdHa-vasishtha)
        db_path: path to palakudu.db
        provider_name: provider key from PROVIDER_REGISTRY (e.g. "ollama")
        provider_config: dict of provider kwargs (model, endpoint, temperature, timeout)
    """
    from madhu.observability.jsonl import RunLogger
    logger = RunLogger(log_path) if log_path is not None else None
    worker = HamsaWorker(
        ticket_id=ticket_id,
        agent_name=agent_name,
        db_path=db_path,
        provider_name=provider_name,
        provider_config=provider_config or {},
        logger=logger,
    )
    worker.run()