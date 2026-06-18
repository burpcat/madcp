# madhu/workers/gemma.py
from __future__ import annotations
from madhu.schemas.payloads import FunctionSpec


"""
Gemma worker — Hamsa-tier leaf worker for MadCP.

Handles function_spec payloads. Calls Ollama, validates the response,
writes the result. One ticket per process (MTap).

No salvage from alpha — built from scratch per stage 9 spec.

Entry point for multiprocessing.Process:
    run_worker(ticket_id, agent_name, db_path)
"""

import ast
import json
import re
import sys
from datetime import datetime, timezone

import httpx

from madhu.workers.base import BaseWorker, WorkerFailure, WorkerResult


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0"
OLLAMA_TIMEOUT = 120.0  # seconds
OLLAMA_TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(payload: dict) -> str:
    """
    Build the Ollama prompt from a function_spec payload dict.

    Instructs Gemma to output exactly one Python function definition —
    no prose, no markdown fences, no explanations.
    """
    name = payload.get("function_name", "")
    signature = payload.get("signature", "")
    docstring = payload.get("docstring", "")
    constraints = payload.get("constraints", [])
    examples = payload.get("examples", [])
    imports_allowed = payload.get("imports_allowed", [])

    constraint_block = "\n".join(f"- {c}" for c in constraints) if constraints else "none"
    example_block = "\n".join(
        f"  input: {ex.get('input', '')}  →  output: {ex.get('output', '')}"
        for ex in examples
    ) if examples else "  (none)"

    imports_block = ", ".join(imports_allowed) if imports_allowed else "none"

    return f"""You are a Python function generator. Output ONLY a single Python function definition.
Do not include any prose, explanation, markdown code fences, or comments outside the function.
Do not define more than one function.
The function must match this signature exactly: {signature}

Function name: {name}
Docstring: {docstring}

Constraints:
{constraint_block}

Examples:
{example_block}

Allowed imports (only these, at the top of the function body if needed): {imports_block}

Output the function definition now:"""


# ---------------------------------------------------------------------------
# Response cleaning and validation
# ---------------------------------------------------------------------------

def _strip_channel_markers(text: str) -> str:
    """
    Remove <|channel|> style markers that Gemma sometimes emits.

    Pattern: <|anything|> at start/end of lines.
    """
    return re.sub(r"<\|[^|]*\|>", "", text).strip()


def _strip_code_fences(text: str) -> str:
    """
    Strip markdown code fences if Gemma wraps its output.

    Handles ```python ... ``` and ``` ... ```.
    """
    # Remove opening fence (with optional language tag)
    text = re.sub(r"^```(?:python)?\s*\n?", "", text.strip())
    # Remove closing fence
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


def _validate_single_function(code: str, expected_name: str) -> str:
    """
    AST-parse code and confirm it contains exactly one function definition
    with the expected name. Returns the cleaned code on success.

    Raises WorkerFailure if:
    - code is not valid Python
    - code contains zero or more than one function definition
    - the function name does not match expected_name
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
        and node.col_offset == 0  # top-level only
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
# Ollama call
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> str:
    """
    Call Ollama synchronously and return the response text.

    Uses httpx in sync mode — workers run in separate processes, not
    an async event loop. stream=false means we wait for the full response.

    Raises WorkerFailure on:
    - HTTP error
    - Timeout
    - Empty response
    """
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": OLLAMA_TEMPERATURE,
        },
    }

    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            response = client.post(OLLAMA_URL, json=payload)
            response.raise_for_status()
    except httpx.TimeoutException:
        raise WorkerFailure(
            reason=f"Ollama request timed out after {OLLAMA_TIMEOUT}s",
            raw_excerpt="",
        )
    except httpx.HTTPStatusError as exc:
        raise WorkerFailure(
            reason=f"Ollama HTTP error: {exc.response.status_code}",
            raw_excerpt=exc.response.text[:500],
        )
    except httpx.RequestError as exc:
        raise WorkerFailure(
            reason=f"Ollama connection error: {exc}",
            raw_excerpt="",
        )

    data = response.json()
    text = data.get("response", "").strip()

    if not text:
        raise WorkerFailure(
            reason="Ollama returned empty response",
            raw_excerpt="",
        )

    return text


# ---------------------------------------------------------------------------
# Worker class
# ---------------------------------------------------------------------------

class GemmaWorker(BaseWorker):
    """
    Hamsa-tier worker. Calls Gemma via Ollama to implement a function_spec.

    MTap: spawned fresh per ticket, exits after one execution.
    Called by the scheduler (stage 11) via run_worker().
    """

    def execute(self, store) -> WorkerResult:
        ticket = store.read(self.ticket_id)
        if ticket is None:
            raise WorkerFailure(
                reason=f"Ticket {self.ticket_id!r} not found in store",
                raw_excerpt="",
            )

        payload = ticket.payload

        # payload may be a FunctionSpec instance (from store.read()) or a dict
        # (from direct construction in tests). Normalise to FunctionSpec.
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
                reason=f"GemmaWorker only handles FunctionSpec payloads, got {type(payload).__name__}",
                raw_excerpt="",
            )

        function_name = payload.function_name
        prompt = _build_prompt(payload)

        raw_response = _call_ollama(prompt)
        cleaned = _strip_channel_markers(raw_response)
        cleaned = _strip_code_fences(cleaned)
        code = _validate_single_function(cleaned, function_name)

        return WorkerResult(
            data=code,
            summary=f"implemented {function_name}() — {len(code)} chars",
        )

# ---------------------------------------------------------------------------
# Multiprocessing entry point
# ---------------------------------------------------------------------------

def run_worker(ticket_id: str, agent_name: str, db_path: str) -> None:
    """
    Entry point for multiprocessing.Process.

    This is a module-level function (not a method) so it can be pickled
    by multiprocessing on all platforms including macOS (spawn context).

    Instantiates GemmaWorker and calls run(). All error handling is
    inside BaseWorker.run() — this function does not catch exceptions.
    Unhandled exceptions will be printed to stderr by the child process
    and the process will exit with a non-zero code. The scheduler (stage 11)
    detects non-zero exit and can take corrective action.
    """
    worker = GemmaWorker(ticket_id=ticket_id, agent_name=agent_name, db_path=db_path)
    worker.run()
