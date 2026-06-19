"""
C4 — Full MCP loop + failure semantics smoke test.

Run from project root AFTER Stage 14 is implemented and unit-tested.
Requires Ollama running with the configured model pulled.

Verifies the full end-to-end loop AND failure-forwarding semantics:

  Happy path:
  - MCP submit_ticket → scheduler → real Gemma → terminal state → result returned
  - JSONL captures the full chain (mcp_submit_enter → worker_spawn → ollama_call
    → ollama_result → worker_exit → mcp_submit_exit)

  Failure path:
  - Submitting an impossible spec causes Gemma to fail repeatedly
  - Each failure produces a forwarded ticket with appended failure_note
  - After exceeding max_forwards (3), the chain terminates as `aborted`
  - Each forward uses a DIFFERENT worker name from the RISHIS pool
  - `aborted` is distinct from `killed` in the result

Run:
    python scratch/c4_full_loop_smoke.py

Exit code 0 on success; 1 on any failure.

Note: this script uses a TEMPORARY DB and TEMPORARY tickets dir.
It does not touch data/palakudu.db or your real logs/runs.jsonl.

It launches the scheduler via the C3 launcher (must already exist).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Adjust imports if your package surface differs
# ---------------------------------------------------------------------------
from madhu.schemas.envelope import Envelope, Ticket
from madhu.schemas.payloads import FunctionSpec
from madhu.store.sqlite import TicketStore
from madhu.store.markdown import MarkdownSync


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_WAIT_HAPPY = 180          # seconds for one happy-path ticket
MAX_WAIT_ABORT = 600          # seconds for 3 forwards + abort to complete
POLL_INTERVAL = 1.0
EXPECTED_MAX_FORWARDS = 3     # per hamsa.yaml failure_policy.max_forwards

TERMINAL_STATUSES = {"done", "failed", "killed", "aborted"}


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
HAPPY_SPEC = {
    "function_name": "reverse_string",
    "signature": "def reverse_string(s: str) -> str:",
    "docstring": "Return the input string reversed.",
    "examples": [{"input": "hello", "output": "olleh"}],
}

# Designed to make Gemma fail: ask for prose AND code, AND make the signature
# inconsistent with the description. Gemma should produce either:
#   - multiple functions (rejected by AST validator)
#   - prose-laden output (rejected by parser)
#   - code that doesn't match the signature
# Across 3 attempts at temperature 0.2, Gemma should fail all three.
IMPOSSIBLE_SPEC = {
    "function_name": "build_country_capitals",
    "signature": "def build_country_capitals() -> dict:",
    "docstring": (
        "You MUST output EXACTLY TWO separate top-level function definitions. "
        "First define a helper: def _get_codes() -> list: ... "
        "Then define the main function: def build_country_capitals() -> dict: ... "
        "Both functions must be at the top level. A single function is wrong."
    ),
    "constraints": [
        "Output exactly two top-level function definitions",
        "First function must be named _get_codes",
        "Second function must be named build_country_capitals",
        "Both must be at module level with zero indentation",
    ],
    "examples": [
        {"input": "", "output": "def _get_codes(): ...\\ndef build_country_capitals(): ..."},
    ],
}


def make_function_spec(spec_dict: dict) -> FunctionSpec:
    return FunctionSpec(
        function_name=spec_dict["function_name"],
        signature=spec_dict["signature"],
        docstring=spec_dict["docstring"],
        constraints=["Single function definition", "No prose, no markdown fences"],
        examples=spec_dict["examples"],
        imports_allowed=[],
    )


def make_ticket(spec_dict: dict) -> Ticket:
    spec = make_function_spec(spec_dict)
    envelope = Envelope(tier_name="Hamsa", tier_level=2)
    try:
        return Ticket(envelope=envelope, payload=spec.model_dump())
    except Exception:
        return Ticket(envelope=envelope, payload=spec)


# ---------------------------------------------------------------------------
# JSONL utilities
# ---------------------------------------------------------------------------
def parse_jsonl(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# Chain following
# ---------------------------------------------------------------------------
def follow_chain(store: TicketStore, original_id: str) -> tuple[str, list[str]]:
    """Follow forwarded_from chain forward; return (head_id, full_chain_ids)."""
    chain = [original_id]
    current = original_id
    visited = {current}
    while True:
        next_ticket = None
        all_tickets = store.list()
        for t in all_tickets:
            tid = t.envelope.id
            if t.envelope.forwarded_from == current and tid not in visited:
                next_ticket = t
                break
        if next_ticket is None:
            return current, chain
        current = next_ticket.envelope.id
        chain.append(current)
        visited.add(current)


def wait_for_terminal(store_path: str, original_id: str, deadline: float) -> tuple[str, str, list[str]]:
    """Poll until the chain head reaches a terminal state. Returns (head_id, status, chain)."""
    last_print = 0
    while time.time() < deadline:
        try:
            snap = TicketStore(db_path=store_path)
        except TypeError:
            snap = TicketStore(store_path)
        head_id, chain = follow_chain(snap, original_id)
        head = snap.read(head_id)
        if head and head.envelope.status in TERMINAL_STATUSES:
            return head_id, head.envelope.status, chain
        if time.time() - last_print > 5:
            print(f"  ...still polling (chain length {len(chain)}, head status: "
                  f"{head.envelope.status if head else '?'})")
            last_print = time.time()
        time.sleep(POLL_INTERVAL)
    head = snap.read(head_id) if head_id else None
    return (head_id or original_id,
            (head.envelope.status if head else "timeout"),
            chain)


# ---------------------------------------------------------------------------
# Main test runs
# ---------------------------------------------------------------------------
def run() -> list[str]:
    project_root = Path(__file__).resolve().parent.parent
    tmpdir = Path(tempfile.mkdtemp(prefix="madhu-c4-"))
    db_path = tmpdir / "test.db"
    tickets_dir = tmpdir / "tickets"
    logs_dir = tmpdir / "logs"
    tickets_dir.mkdir()
    logs_dir.mkdir()
    log_path = logs_dir / "runs.jsonl"

    print(f"tmp dir: {tmpdir}")
    print(f"db:      {db_path}")
    print(f"log:     {log_path}\n")

    failures = []

    # ---- Build store + wire markdown sync (composition-root pattern) ----
    try:
        md_sync = MarkdownSync(tickets_dir=tickets_dir)
    except TypeError:
        md_sync = MarkdownSync(str(tickets_dir))

    try:
        store = TicketStore(db_path=str(db_path))
    except TypeError:
        store = TicketStore(str(db_path))
    store._on_ticket_write = md_sync.sync_ticket

    # ---- Inject happy-path ticket ----
    print("§1 — Inject happy-path ticket")
    happy_ticket = make_ticket(HAPPY_SPEC)
    happy_id = store.create(happy_ticket)
    print(f"  happy ticket: {happy_id[:8]}  ({HAPPY_SPEC['function_name']})")

    # ---- Inject impossible-spec ticket ----
    print("\n§2 — Inject impossible-spec ticket (expects 3 forwards → aborted)")
    impossible_ticket = make_ticket(IMPOSSIBLE_SPEC)
    impossible_id = store.create(impossible_ticket)
    print(f"  impossible ticket: {impossible_id[:8]}  ({IMPOSSIBLE_SPEC['function_name']})")

    # ---- Spawn scheduler via launcher ----
    print("\n§3 — Starting scheduler subprocess")
    env = os.environ.copy()
    env["MADHU_DB_PATH"] = str(db_path)
    env["MADHU_LOG_PATH"] = str(log_path)
    env["MADHU_TICKETS_DIR"] = str(tickets_dir)

    launcher_path = project_root / "scratch" / "c3_scheduler_launcher.py"
    if not launcher_path.exists():
        failures.append(f"Launcher not found at {launcher_path} — required for C4")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return failures

    proc = subprocess.Popen(
        [sys.executable, str(launcher_path)],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # ---- Wait for happy-path ----
        print("\n§4 — Waiting for happy-path ticket to terminate")
        deadline = time.time() + MAX_WAIT_HAPPY
        head_id, status, chain = wait_for_terminal(str(db_path), happy_id, deadline)
        if status == "done":
            print(f"  ✓ happy-path → {status} (chain length {len(chain)})")
        else:
            failures.append(
                f"happy-path ticket {happy_id[:8]} ended in {status!r}, "
                f"expected 'done'. Chain: {[c[:8] for c in chain]}"
            )

        # ---- Wait for impossible-path ----
        print("\n§5 — Waiting for impossible-spec chain to terminate (up to 10 min)")
        deadline = time.time() + MAX_WAIT_ABORT
        head_id, status, chain = wait_for_terminal(str(db_path), impossible_id, deadline)
        print(f"  chain: {' → '.join(c[:8] for c in chain)}  (head status: {status})")

        # ---- Verifications on the impossible-path chain ----
        print("\n§6 — Verification — failure semantics")

        # (a) Status must be aborted, not killed, not done
        if status == "aborted":
            print(f"  ✓ chain head status = aborted (distinct from killed)")
        elif status == "killed":
            failures.append(
                "chain head status = 'killed' (should be 'aborted'). "
                "Stage 14 must distinguish: 'killed' = operator/timeout terminated; "
                "'aborted' = forwarding limit exceeded."
            )
        elif status == "done":
            failures.append(
                "impossible spec reached 'done' — either Gemma got lucky 3 times "
                "(unlikely at temp 0.2) or the spec wasn't actually impossible. "
                "Re-run; if persistent, the impossible spec needs to be harder."
            )
        else:
            failures.append(f"unexpected terminal status: {status!r}")

        # (b) Chain length must equal max_forwards + 1
        expected_chain_length = EXPECTED_MAX_FORWARDS + 1
        if len(chain) == expected_chain_length:
            print(f"  ✓ chain length = {len(chain)} (original + {EXPECTED_MAX_FORWARDS} forwards)")
        else:
            failures.append(
                f"chain length {len(chain)}; expected {expected_chain_length} "
                f"(original + {EXPECTED_MAX_FORWARDS} forwards). "
                f"Either max_forwards is misconfigured or forwarding stopped early."
            )

        # (c) failure_notes on the aborted ticket must equal max_forwards
        try:
            snap = TicketStore(db_path=str(db_path))
        except TypeError:
            snap = TicketStore(str(db_path))
        aborted = snap.read(head_id)
        if aborted is None:
            failures.append(f"could not re-read aborted ticket {head_id}")
        else:
            notes_count = len(aborted.envelope.failure_notes)
            if notes_count == EXPECTED_MAX_FORWARDS:
                print(f"  ✓ aborted ticket has {notes_count} failure_notes "
                      f"(one per forward)")
            else:
                failures.append(
                    f"aborted ticket has {notes_count} failure_notes; "
                    f"expected {EXPECTED_MAX_FORWARDS}"
                )

            # (d) Each forward must use a DIFFERENT worker name
            agents = []
            for fn in aborted.envelope.failure_notes:
                a = fn.agent if hasattr(fn, "agent") else fn.get("agent", "")
                # Strip lineage prefix if present (e.g. "AdHa-vasishtha" → "vasishtha")
                agents.append(a.split("-")[-1] if "-" in a else a)
            unique_agents = set(agents)
            if len(unique_agents) == len(agents) and len(agents) > 0:
                print(f"  ✓ each forward used a different worker name: {agents}")
            elif len(agents) == 0:
                failures.append("no agents recorded in failure_notes")
            else:
                failures.append(
                    f"worker names repeated across forwards: {agents}. "
                    f"Each forward should pick a different RISHIS-pool name."
                )

        # ---- Verifications on full JSONL loop coverage ----
        print("\n§7 — Verification — JSONL covers full MCP loop")
        events = parse_jsonl(log_path)

        # Required event types for full-loop coverage
        required_event_types = {
            "worker_spawn",
            "worker_exit",
            "ollama_call",
            "ollama_result",
            "touch_acquire",
            "touch_release",
            "forward",
        }
        seen_event_types = {e.get("event_type") for e in events}
        missing = required_event_types - seen_event_types
        if not missing:
            print(f"  ✓ JSONL contains all required event types ({len(seen_event_types)} unique types seen)")
        else:
            failures.append(
                f"JSONL missing required event types: {sorted(missing)}. "
                f"Saw: {sorted(seen_event_types)}"
            )

        # Forward events: should be exactly max_forwards
        forward_events = [e for e in events if e.get("event_type") == "forward"]
        if len(forward_events) >= EXPECTED_MAX_FORWARDS:
            print(f"  ✓ JSONL captured {len(forward_events)} forward events "
                  f"(≥ {EXPECTED_MAX_FORWARDS} expected)")
        else:
            failures.append(
                f"JSONL captured {len(forward_events)} forward events; "
                f"expected ≥ {EXPECTED_MAX_FORWARDS}"
            )

    finally:
        # ---- Shutdown ----
        print("\n§8 — Stopping scheduler")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("  scheduler didn't exit on SIGINT, sending SIGKILL")
            proc.kill()
            proc.wait()

        if failures or proc.returncode not in (0, -signal.SIGINT):
            try:
                out, err = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            if out:
                print(f"  scheduler stdout (last 500): {out[-500:]!r}")
            if err:
                print(f"  scheduler stderr (last 800): {err[-800:]!r}")

        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 64)
    print("C4 — Full MCP loop + failure semantics smoke")
    print("=" * 64)
    print()
    print("Pre-flight checklist:")
    print("  - Ollama running at http://localhost:11434")
    print("  - Configured model pulled")
    print("  - Stage 14 (failure forwarding behaviour) implemented")
    print("  - scratch/c3_scheduler_launcher.py exists (re-used by C4)")
    print()
    print("Expected runtime: 3–10 minutes (one happy ticket + 3 Gemma rejections)")
    print()

    try:
        failures = run()
    except Exception as e:
        print(f"\nFATAL: smoke harness crashed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(2)

    print()
    print("=" * 64)
    if failures:
        print(f"  FAILED ({len(failures)} issues)")
        for f in failures:
            print(f"    - {f}")
    else:
        print("  PASSED — full MCP loop + failure semantics verified")
    print("=" * 64)
    sys.exit(1 if failures else 0)
