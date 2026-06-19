"""
C3 — Live worker pipeline smoke test.

Run from project root AFTER Stage 11 is implemented and unit-tested.
Requires Ollama running with the configured model pulled.

Verifies the live worker pipeline:

  - Real Gemma generates code via the worker
  - Workers spawn as multiprocessing.Process (fresh PID per ticket → MTap)
  - Scheduler respects Hamsa max_parallel
  - Lineage paths follow AdHa-{name} format
  - All tickets reach a terminal state (done | forwarded | aborted)
  - JSONL log captures worker_spawn / worker_exit events

Run:
    python scratch/c3_worker_pipeline_smoke.py

Exit code 0 on success; 1 on any failure.

Note: this script uses a TEMPORARY DB. It does not touch data/palakudu.db.
Stage 11.5 (scheduler resilience) is NOT required for this test to pass —
this is a happy-path integration check.
"""
from __future__ import annotations

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
NUM_TICKETS = 5
MAX_WAIT_SECONDS = 600  # 10 minutes for all 5 tickets to terminate
POLL_INTERVAL = 1.0
EXPECTED_MAX_PARALLEL = 2  # Hamsa max_parallel from hamsa.yaml

TERMINAL_STATUSES = {"done", "failed", "killed", "aborted"}
# "forwarded" is not terminal — it spawns a new ticket. We track the *chain*.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SPECS = [
    {
        "function_name": "reverse_string",
        "signature": "def reverse_string(s: str) -> str:",
        "docstring": "Return the input string reversed.",
        "examples": [{"input": "hello", "output": "olleh"}],
    },
    {
        "function_name": "to_uppercase",
        "signature": "def to_uppercase(s: str) -> str:",
        "docstring": "Return the input string with all characters uppercased.",
        "examples": [{"input": "hello", "output": "HELLO"}],
    },
    {
        "function_name": "strip_whitespace",
        "signature": "def strip_whitespace(s: str) -> str:",
        "docstring": "Return the input string with leading and trailing whitespace removed.",
        "examples": [{"input": "  hello  ", "output": "hello"}],
    },
    {
        "function_name": "last_word",
        "signature": "def last_word(s: str) -> str:",
        "docstring": "Return the last whitespace-separated word in the input string.",
        "examples": [{"input": "hello world", "output": "world"}],
    },
    {
        "function_name": "double_string",
        "signature": "def double_string(s: str) -> str:",
        "docstring": "Return the input string concatenated with itself.",
        "examples": [{"input": "ab", "output": "abab"}],
    },
]

def make_ticket(spec_dict: dict) -> Ticket:
    spec = FunctionSpec(
        function_name=spec_dict["function_name"],
        signature=spec_dict["signature"],
        docstring=spec_dict["docstring"],
        constraints=["Single function definition", "No prose, no markdown fences"],
        examples=spec_dict["examples"],
        imports_allowed=[],
    )
    envelope = Envelope(tier_name="Hamsa", tier_level=2)
    try:
        return Ticket(envelope=envelope, payload=spec.model_dump())
    except Exception:
        return Ticket(envelope=envelope, payload=spec)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def follow_chain(store: TicketStore, original_id: str) -> str:
    """Follow forwarded_from chain forward from original_id to find current head."""
    # Find tickets whose forwarded_from == original_id (depth-first)
    current = original_id
    visited = {current}
    while True:
        # naive linear scan; fine for ≤5 tickets
        next_ticket = None
        all_tickets = store.list()
        for t in all_tickets:
            if t.envelope.forwarded_from == current and t.envelope.id not in visited:
                next_ticket = t
                break
        if next_ticket is None:
            return current
        current = next_ticket.envelope.id
        visited.add(current)


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


def reconstruct_max_concurrent(events: list[dict]) -> int:
    """From worker_spawn / worker_exit events, find peak concurrent worker count."""
    active = 0
    peak = 0
    # Sort by timestamp (string-sortable ISO-8601)
    for ev in sorted(events, key=lambda e: e.get("timestamp", "")):
        et = ev.get("event_type", "")
        if et == "worker_spawn":
            active += 1
            peak = max(peak, active)
        elif et == "worker_exit":
            active = max(0, active - 1)
    return peak


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    project_root = Path(__file__).resolve().parent.parent
    tmpdir = Path(tempfile.mkdtemp(prefix="madhu-c3-"))
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

    # -----------------------------------------------------------------
    # Step 1: inject N tickets directly into a fresh SQLite
    # -----------------------------------------------------------------
    print(f"§1 — Injecting {NUM_TICKETS} tickets into fresh DB")
    try:
        md_sync = MarkdownSync(tickets_dir=tickets_dir)
    except TypeError:
        md_sync = MarkdownSync(str(tickets_dir))

    try:
        store = TicketStore(db_path=str(db_path), markdown_sync=md_sync)
    except TypeError:
        store = TicketStore(str(db_path))

    original_ids = []
    for spec in SPECS[:NUM_TICKETS]:
        t = make_ticket(spec)
        tid = store.create(t)
        original_ids.append(tid)
        print(f"  injected {tid[:8]}  ({spec['function_name']})")

    # -----------------------------------------------------------------
    # Step 2: spawn scheduler as subprocess pointing at our tmp DB + log
    # -----------------------------------------------------------------
    print("\n§2 — Starting scheduler subprocess")
    env = os.environ.copy()
    env["MADHU_DB_PATH"] = str(db_path)
    env["MADHU_LOG_PATH"] = str(log_path)
    env["MADHU_TICKETS_DIR"] = str(tickets_dir)
    # If your scheduler reads config differently, set env vars accordingly,
    # or invoke via a small launcher script you write in scratch/.

    # Adjust this if your scheduler isn't directly runnable as a module
    scheduler_cmd = [sys.executable, "-m", "madhu.scheduler"]
    scheduler_cmd = [sys.executable, "scratch/c3_scheduler_launcher.py"]

    proc = subprocess.Popen(
        scheduler_cmd,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # -------------------------------------------------------------
        # Step 3: poll until all chains terminate or timeout
        # -------------------------------------------------------------
        print(f"\n§3 — Polling for terminal states (timeout {MAX_WAIT_SECONDS}s)")
        deadline = time.time() + MAX_WAIT_SECONDS
        terminal_ids = {}  # original_id -> head_id at terminal status
        last_print = 0

        while time.time() < deadline:
            # Re-open store fresh each loop (avoids stale read cache)
            try:
                snapshot_store = TicketStore(str(db_path))
            except TypeError:
                snapshot_store = store

            terminal_ids.clear()
            for orig in original_ids:
                head_id = follow_chain(snapshot_store, orig)
                head = snapshot_store.read(head_id)
                if head and head.envelope.status in TERMINAL_STATUSES:
                    terminal_ids[orig] = (head_id, head.envelope.status)

            if time.time() - last_print > 5:
                print(f"  {len(terminal_ids)}/{NUM_TICKETS} terminal "
                      f"after {int(time.time() - (deadline - MAX_WAIT_SECONDS))}s")
                last_print = time.time()

            if len(terminal_ids) == NUM_TICKETS:
                break

            time.sleep(POLL_INTERVAL)

        # -------------------------------------------------------------
        # Step 4: assertions
        # -------------------------------------------------------------
        print(f"\n§4 — Verification")

        if len(terminal_ids) != NUM_TICKETS:
            failures.append(
                f"Only {len(terminal_ids)}/{NUM_TICKETS} tickets reached terminal "
                f"state within {MAX_WAIT_SECONDS}s"
            )
        else:
            print(f"  ✓ All {NUM_TICKETS} tickets reached a terminal state")

        # Status breakdown
        status_counts: dict[str, int] = {}
        for orig, (head, status) in terminal_ids.items():
            status_counts[status] = status_counts.get(status, 0) + 1
        for s, c in sorted(status_counts.items()):
            print(f"    {s}: {c}")

        # JSONL inspection
        events = parse_jsonl(log_path)
        spawn_events = [e for e in events if e.get("event_type") == "worker_spawn"]
        exit_events = [e for e in events if e.get("event_type") == "worker_exit"]

        if not spawn_events:
            failures.append("No worker_spawn events in JSONL log — scheduler may "
                            "not be wired to log, or workers never spawned")
        else:
            print(f"  ✓ JSONL captured {len(spawn_events)} spawn events, "
                  f"{len(exit_events)} exit events")

        # MTap: distinct PIDs per spawn (no reuse)
        pids = [e.get("details", {}).get("pid") for e in spawn_events if e.get("details", {}).get("pid")]
        if pids and len(set(pids)) != len(pids):
            failures.append(f"Worker PIDs reused (MTap violation): {pids}")
        elif pids:
            print(f"  ✓ MTap: all {len(pids)} worker PIDs distinct")
        else:
            print("  ⚠ Could not verify MTap — no 'pid' field in spawn event details")

        # max_parallel cap
        peak = reconstruct_max_concurrent(events)
        if peak > EXPECTED_MAX_PARALLEL:
            failures.append(
                f"Peak concurrent workers {peak} exceeded Hamsa max_parallel "
                f"{EXPECTED_MAX_PARALLEL}"
            )
        else:
            print(f"  ✓ Peak concurrent workers: {peak} ≤ {EXPECTED_MAX_PARALLEL}")

        # Lineage path format
        agent_names = [e.get("agent_name", "") for e in spawn_events]
        bad_lineage = [a for a in agent_names if not a.startswith("AdHa-")]
        if bad_lineage:
            failures.append(f"Lineage paths don't match AdHa-* format: {bad_lineage[:3]}")
        elif agent_names:
            print(f"  ✓ Lineage paths: all {len(agent_names)} match AdHa-* format")

        # Lowercase leaf-tier names
        worker_part = [a.split("-", 1)[1] for a in agent_names if "-" in a]
        bad_case = [n for n in worker_part if n != n.lower()]
        if bad_case:
            failures.append(f"Leaf-tier worker names not lowercase: {bad_case[:3]}")
        elif worker_part:
            print(f"  ✓ Leaf-tier worker names lowercase")

    finally:
        # -------------------------------------------------------------
        # Step 5: tear down scheduler
        # -------------------------------------------------------------
        print("\n§5 — Stopping scheduler")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("  scheduler didn't exit on SIGINT, sending SIGKILL")
            proc.kill()
            proc.wait()

        # Capture scheduler output for diagnostics if anything went wrong
        if failures or proc.returncode not in (0, -signal.SIGINT):
            out, err = proc.communicate() if proc.stdout else (b"", b"")
            if out:
                print(f"  scheduler stdout (last 500 chars): {out[-500:]!r}")
            if err:
                print(f"  scheduler stderr (last 500 chars): {err[-500:]!r}")

        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 64)
    print("C3 — Live worker pipeline smoke")
    print("=" * 64)
    print()
    print("Pre-flight checklist:")
    print("  - Ollama running at http://localhost:11434")
    print("  - Configured model pulled")
    print("  - Stage 11 (scheduler) implemented")
    print("  - `python -m madhu.scheduler` is the correct invocation")
    print("    (edit scheduler_cmd in this script if not)")
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
        print("  PASSED — live worker pipeline verified")
    print("=" * 64)
    sys.exit(1 if failures else 0)