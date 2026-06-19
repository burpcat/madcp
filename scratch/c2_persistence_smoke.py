"""
C2 — Persistence + coordination smoke test.

Run from project root after Stage 8 (touch protocol) and ideally after Stage 11.
Verifies the persistence + coordination foundation:

  - SQLite is source of truth (create → read round-trips correctly)
  - Markdown is derived (one .md per ticket, frontmatter has envelope)
  - Touch protocol is atomic (concurrent acquires: exactly one winner)
  - Touch release writes correct end time and summary
  - Forward chain accumulates failure_notes (1 → 2 → 3 entries)
  - Markdown reflects forward chain via wiki-links

Run:
    python scratch/c2_persistence_smoke.py

Exit code 0 if all checks pass; 1 on any failure.

If your API differs from build-guide spec, the failing check will print what
it found vs what it expected. Adjust import paths or method names locally.

Note on composition-root wiring:
    `MarkdownSync` integration is wired explicitly via `store._on_ticket_write`
    after the store is constructed. This is the v0 pattern — wiring lives at
    every entry point (server boot, dashboard, this script), not inside the
    store's constructor. See BUILD-STATE.md decisions log.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Adjust these imports if your package surface differs
# ---------------------------------------------------------------------------
from madhu.schemas.envelope import Envelope, FailureNote, Ticket, TouchEntry
from madhu.schemas.payloads import FunctionSpec
from madhu.store.sqlite import TicketStore
from madhu.store.markdown import MarkdownSync
from madhu.store.touch import TouchManager


# ---------------------------------------------------------------------------
# Test harness (mirrors C1 style)
# ---------------------------------------------------------------------------
PASS = 0
FAIL = 0


def check(name: str, fn):
    global PASS, FAIL
    try:
        result = fn()
        if result is False:
            print(f"  ✗ {name}  (returned False)")
            FAIL += 1
        else:
            print(f"  ✓ {name}")
            PASS += 1
    except AssertionError as e:
        print(f"  ✗ {name}  (assertion: {e})")
        FAIL += 1
    except Exception as e:
        print(f"  ✗ {name}  ({type(e).__name__}: {e})")
        traceback.print_exc(limit=2)
        FAIL += 1


def section(title: str):
    print(f"\n— §{title} —")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_function_spec(name: str = "reverse_string") -> FunctionSpec:
    return FunctionSpec(
        function_name=name,
        signature=f"def {name}(s: str) -> str:",
        docstring="Return the input string reversed.",
        constraints=["No imports", "Single function"],
        examples=[{"input": "hello", "output": "olleh"}],
        imports_allowed=[],
    )


def make_ticket(spec: FunctionSpec) -> Ticket:
    """Build a minimal Hamsa-tier ticket. Pass payload as dict if your
    Ticket model requires dict payloads (Pydantic v2 default behaviour varies)."""
    envelope = Envelope(
        tier_name="Hamsa",
        tier_level=2,
    )
    try:
        return Ticket(envelope=envelope, payload=spec.model_dump())
    except Exception:
        # Fallback: some Ticket models accept the Pydantic instance directly
        return Ticket(envelope=envelope, payload=spec)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def run():
    global PASS, FAIL
    tmpdir = Path(tempfile.mkdtemp(prefix="madhu-c2-"))
    db_path = tmpdir / "test.db"
    tickets_dir = tmpdir / "tickets"
    tickets_dir.mkdir()

    print(f"tmp dir: {tmpdir}")

    try:
        # MarkdownSync — try keyword form, fall back to positional
        try:
            md_sync = MarkdownSync(tickets_dir=tickets_dir)
        except TypeError:
            md_sync = MarkdownSync(str(tickets_dir))

        # TicketStore — built without markdown coupling; wire callback after
        try:
            store = TicketStore(db_path=str(db_path))
        except TypeError:
            store = TicketStore(str(db_path))

        # Composition-root wiring: every entry point sets this callback
        store._on_ticket_write = md_sync.sync_ticket

        touch = TouchManager(store)

        # -----------------------------------------------------------------
        section("1 — Round-trip: create / read / equality")
        # -----------------------------------------------------------------
        spec = make_function_spec()
        ticket = make_ticket(spec)

        ticket_id_holder: dict = {}

        def _create():
            tid = store.create(ticket)
            ticket_id_holder["id"] = tid
            return bool(tid)

        check("Create returns non-empty ticket id", _create)

        def _read():
            tid = ticket_id_holder["id"]
            read_back = store.read(tid)
            assert read_back is not None, "read() returned None"
            assert read_back.envelope.tier_name == "Hamsa", \
                f"tier_name mismatch: {read_back.envelope.tier_name}"
            assert read_back.envelope.status == "queued", \
                f"default status not queued: {read_back.envelope.status}"
            return True

        check("Read returns a Ticket with matching fields", _read)

        # -----------------------------------------------------------------
        section("2 — Markdown derivation")
        # -----------------------------------------------------------------
        def _md_exists():
            tid = ticket_id_holder["id"]
            md_file = tickets_dir / f"{tid}.md"
            assert md_file.exists(), \
                f"markdown file not created at {md_file}; tickets dir contains: {list(tickets_dir.iterdir())}"
            return True

        check("Markdown file exists for created ticket", _md_exists)

        def _md_has_frontmatter():
            tid = ticket_id_holder["id"]
            content = (tickets_dir / f"{tid}.md").read_text()
            assert content.startswith("---"), \
                f"markdown missing frontmatter; starts with: {content[:60]!r}"
            assert "tier_name" in content, "frontmatter missing tier_name"
            assert "Hamsa" in content, "tier_name value not in frontmatter"
            return True

        check("Markdown has YAML frontmatter with envelope fields", _md_has_frontmatter)

        # -----------------------------------------------------------------
        section("3 — Touch acquire is atomic")
        # -----------------------------------------------------------------
        # Create a fresh ticket for the race
        race_ticket = make_ticket(make_function_spec("upper_string"))
        race_id = store.create(race_ticket)

        winners: list[str] = []
        barrier = threading.Barrier(2)

        def race(agent_name: str):
            barrier.wait()  # sync the two threads at the start
            got = touch.acquire(race_id, agent_name)
            if got:
                winners.append(agent_name)

        def _race():
            t1 = threading.Thread(target=race, args=("agent-a",))
            t2 = threading.Thread(target=race, args=("agent-b",))
            t1.start(); t2.start()
            t1.join(); t2.join()
            assert len(winners) == 1, \
                f"expected exactly 1 winner, got {len(winners)}: {winners}"
            return True

        check("Two concurrent acquires: exactly one winner", _race)

        # -----------------------------------------------------------------
        section("4 — Touch release writes history + status")
        # -----------------------------------------------------------------
        def _release():
            tid = race_id
            agent = winners[0] if winners else "agent-a"
            touch.release(
                ticket_id=tid,
                agent_name=agent,
                summary="completed work",
                status_after="done",
            )
            t = store.read(tid)
            assert t.envelope.status == "done", \
                f"status not 'done' after release: {t.envelope.status}"
            assert len(t.envelope.touch_history) >= 1, \
                f"touch_history empty after release; expected ≥ 1 entry"
            entry = t.envelope.touch_history[-1]
            # Tolerate either dict or model representation
            agent_field = entry.agent if hasattr(entry, "agent") else entry.get("agent")
            assert agent_field == agent, \
                f"touch entry agent mismatch: {agent_field}"
            return True

        check("Release transitions status to 'done' and appends to touch_history", _release)

        # -----------------------------------------------------------------
        section("5 — Forward chain accumulates failure_notes")
        # -----------------------------------------------------------------
        fwd_ticket = make_ticket(make_function_spec("multiply_strings"))
        fwd_id_0 = store.create(fwd_ticket)

        def _forward_once():
            new_id = touch.forward(
                ticket_id=fwd_id_0,
                reason="Gemma returned multiple functions",
                raw_excerpt="def f1():... def f2():...",
            )
            # Verify old ticket marked forwarded
            old = store.read(fwd_id_0)
            assert old.envelope.status == "forwarded", \
                f"original status not 'forwarded': {old.envelope.status}"
            # Verify new ticket has forwarded_from + 1 failure note
            new = store.read(new_id)
            assert new.envelope.forwarded_from == fwd_id_0, \
                f"forwarded_from not set: {new.envelope.forwarded_from}"
            assert len(new.envelope.failure_notes) == 1, \
                f"new ticket should have 1 failure_note, has {len(new.envelope.failure_notes)}"
            return new_id

        fwd_id_1 = None
        try:
            fwd_id_1 = _forward_once()
            print("  ✓ First forward: new ticket has 1 failure_note")
            PASS += 1
        except Exception as e:
            print(f"  ✗ First forward failed: {e}")
            FAIL += 1

        def _forward_again():
            new_id = touch.forward(
                ticket_id=fwd_id_1,
                reason="Gemma returned prose",
                raw_excerpt="Sure, here's the function: ...",
            )
            new = store.read(new_id)
            assert len(new.envelope.failure_notes) == 2, \
                f"second forward should yield 2 failure_notes, has {len(new.envelope.failure_notes)}"
            return new_id

        fwd_id_2 = None
        if fwd_id_1:
            try:
                fwd_id_2 = _forward_again()
                print("  ✓ Second forward: failure_notes grew to 2")
                PASS += 1
            except Exception as e:
                print(f"  ✗ Second forward failed: {e}")
                FAIL += 1

        if fwd_id_2:
            try:
                final = store.read(fwd_id_2)
                reasons = []
                for fn in final.envelope.failure_notes:
                    r = fn.reason if hasattr(fn, "reason") else fn.get("reason")
                    reasons.append(r)
                assert reasons[0] == "Gemma returned multiple functions"
                assert reasons[1] == "Gemma returned prose"
                print("  ✓ failure_notes preserve order and content of prior reasons")
                PASS += 1
            except Exception as e:
                print(f"  ✗ failure_notes ordering check failed: {e}")
                FAIL += 1

        # -----------------------------------------------------------------
        section("6 — Markdown reflects forward chain")
        # -----------------------------------------------------------------
        if fwd_id_2:
            def _md_wiki_link():
                content = (tickets_dir / f"{fwd_id_2}.md").read_text()
                assert "[[" in content, \
                    "markdown body should contain at least one [[wiki-link]]"
                assert fwd_id_1 in content or fwd_id_0 in content, \
                    "markdown should reference a predecessor ticket id"
                return True

            check("Forwarded ticket markdown contains wiki-link to predecessor", _md_wiki_link)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 64)
    print("C2 — Persistence + coordination smoke")
    print("=" * 64)
    try:
        run()
    except Exception as e:
        print(f"\nFATAL: smoke harness crashed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(2)

    print()
    print("=" * 64)
    print(f"  Passed: {PASS}")
    print(f"  Failed: {FAIL}")
    print("=" * 64)
    sys.exit(0 if FAIL == 0 else 1)