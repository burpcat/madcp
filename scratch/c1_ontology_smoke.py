#!/usr/bin/env python3
# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# scratch/c1_ontology_smoke.py
"""
MadCP Integration Checkpoint C1+ — Ontology Smoke Test
Covers: Stages 2-5 (envelope, payloads, migrations, naming, SQLite schema)

Run as: python scratch/c1_ontology_smoke.py
Exits 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0
_current_section = ""


def section(title: str) -> None:
    global _current_section
    _current_section = title
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(label: str, fn) -> None:
    """
    Run fn(). If it returns without raising, mark as PASS.
    If it raises, mark as FAIL and print the exception.
    fn may return a string to print as additional context.
    """
    global _passed, _failed
    try:
        result = fn()
        msg = f"  [PASS] {label}"
        if isinstance(result, str):
            msg += f" — {result}"
        print(msg)
        _passed += 1
    except Exception as e:
        print(f"  [FAIL] {label}")
        print(f"         {type(e).__name__}: {e}")
        traceback.print_exc(limit=3, file=sys.stdout)
        _failed += 1


def expect_raises(label: str, exc_type: type, fn) -> None:
    """
    Run fn(). If it raises exc_type (or a subclass), mark as PASS.
    If it raises the wrong exception or doesn't raise at all, mark as FAIL.
    """
    global _passed, _failed
    try:
        fn()
        print(f"  [FAIL] {label} — expected {exc_type.__name__} but nothing was raised")
        _failed += 1
    except exc_type:
        print(f"  [PASS] {label} — raised {exc_type.__name__} as expected")
        _passed += 1
    except Exception as e:
        print(f"  [FAIL] {label} — expected {exc_type.__name__}, got {type(e).__name__}: {e}")
        _failed += 1


# ===========================================================================
# §1 — Imports
# ===========================================================================

section("§1 — Imports")

# We import at check time so failures are reported as FAIL rather than
# crashing the whole script.

envelope_mod = None
payloads_mod = None
migrations_mod = None
naming_mod = None
names_mod = None
sqlite_mod = None


def _import_all():
    global envelope_mod, payloads_mod, migrations_mod
    global naming_mod, names_mod, sqlite_mod
    import madhu.schemas.envelope as e
    import madhu.schemas.payloads as p
    import madhu.schemas.migrations as m
    import madhu.naming as n
    import madhu.names as ns
    import madhu.store.sqlite as s
    envelope_mod  = e
    payloads_mod  = p
    migrations_mod = m
    naming_mod    = n
    names_mod     = ns
    sqlite_mod    = s


check("All six modules import without error", _import_all)


def _krishnas_exists():
    from madhu.names import KRISHNAS
    assert len(KRISHNAS) == 24, f"Expected 24 entries, got {len(KRISHNAS)}"
    assert KRISHNAS[0] == "Adi Purusha", f"First entry wrong: {KRISHNAS[0]}"
    assert KRISHNAS[-1] == "Hamsa", f"Last entry wrong: {KRISHNAS[-1]}"
    return f"24 entries, [{KRISHNAS[0]} … {KRISHNAS[-1]}]"


check("KRISHNAS has 24 entries, starts with Adi Purusha, ends with Hamsa",
      _krishnas_exists)


def _all_pools_exist():
    from madhu.names import (
        KRISHNAS, HEROES, GRAHA, GUARDIANS,
        RISHIS, PEETHAS, VAHANAS,
    )
    pools = {
        "KRISHNAS":  KRISHNAS,
        "HEROES":    HEROES,
        "GRAHA":     GRAHA,
        "GUARDIANS": GUARDIANS,
        "RISHIS":    RISHIS,
        "PEETHAS":   PEETHAS,
        "VAHANAS":   VAHANAS,
    }
    for name, pool in pools.items():
        assert isinstance(pool, list) and len(pool) > 0, \
            f"{name} is empty or not a list"
    return f"all 7 pools present ({', '.join(f'{k}={len(v)}' for k, v in pools.items())})"


check("All 7 pool constants exist and are non-empty lists", _all_pools_exist)


# ===========================================================================
# §2 — Envelope
# ===========================================================================

section("§2 — Envelope")


def _default_agent():
    from madhu.schemas.envelope import Envelope
    env = Envelope(tier_name="Hamsa", tier_level=24)
    assert env.created_by_agent == "param-aatma", \
        f"Expected 'param-aatma', got {env.created_by_agent!r}"
    return f"created_by_agent={env.created_by_agent!r}"


check("Envelope.created_by_agent defaults to 'param-aatma'", _default_agent)


def _mtap_default():
    from madhu.schemas.envelope import Envelope
    env = Envelope(tier_name="Hamsa", tier_level=24)
    assert env.mtap is True, f"Expected True, got {env.mtap}"
    return "mtap=True"


check("Envelope.mtap defaults to True", _mtap_default)


def _empty_lists():
    from madhu.schemas.envelope import Envelope
    env = Envelope(tier_name="Hamsa", tier_level=24)
    assert env.failure_notes == [], f"failure_notes not empty: {env.failure_notes}"
    assert env.touch_history == [], f"touch_history not empty: {env.touch_history}"
    return "failure_notes=[], touch_history=[]"


check("Envelope.failure_notes and touch_history default to []", _empty_lists)


def _all_statuses_valid():
    from madhu.schemas.envelope import Envelope
    statuses = [
        "queued", "touched", "in_progress", "done",
        "failed", "killed", "forwarded", "aborted",
    ]
    for status in statuses:
        env = Envelope(tier_name="Hamsa", tier_level=24, status=status)
        assert env.status == status, f"Status roundtrip failed for {status!r}"
    return f"all {len(statuses)} statuses accepted"


check("All 8 status values are accepted by Envelope", _all_statuses_valid)


def _invalid_status():
    from madhu.schemas.envelope import Envelope
    Envelope(tier_name="Hamsa", tier_level=24, status="banana")

expect_raises(
    "Invalid status 'banana' raises an exception",
    Exception,
    _invalid_status,
)


# check("Invalid status 'banana' raises an exception", _invalid_status)


def _ticket_roundtrip():
    from madhu.schemas.envelope import Envelope, Ticket
    ticket = Ticket(
        envelope=Envelope(tier_name="Hamsa", tier_level=24),
        payload={"type": "function_spec", "name": "smoke_test"},
    )
    raw  = ticket.model_dump_json()
    restored = Ticket.model_validate_json(raw)
    assert restored.envelope.id == ticket.envelope.id, "ID mismatch after roundtrip"
    assert restored.envelope.created_by_agent == "param-aatma"
    assert restored.payload == ticket.payload
    return f"ticket id={ticket.envelope.id[:8]}… survived roundtrip"


check("Ticket model_dump → model_validate roundtrip is lossless", _ticket_roundtrip)


# ===========================================================================
# §3 — FunctionSpec
# ===========================================================================

section("§3 — FunctionSpec")

_VALID_SPEC = dict(
    function_name="reverse_string",
    signature="def reverse_string(s: str) -> str",
    docstring="Return the reverse of the input string.",
    constraints=["must handle empty string", "must not use slicing syntax"],
    examples=[{"input": "reverse_string('hello')", "output": "'olleh'"}],
    imports_allowed=[],
)


def _valid_spec_succeeds():
    from madhu.schemas.payloads import FunctionSpec
    spec = FunctionSpec(**_VALID_SPEC)
    assert spec.function_name == "reverse_string"
    assert spec.type == "function_spec"
    assert spec.schema_version == "1.0"
    return f"type={spec.type!r}, schema_version={spec.schema_version!r}"


check("Valid FunctionSpec constructs without error", _valid_spec_succeeds)


def _reject_uppercase(**kw):
    from madhu.schemas.payloads import FunctionSpec
    from pydantic import ValidationError
    bad = {**_VALID_SPEC, "function_name": "ReverseString",
           "signature": "def ReverseString(s: str) -> str", **kw}
    FunctionSpec(**bad)


expect_raises(
    "function_name='ReverseString' (uppercase) is rejected",
    Exception, _reject_uppercase,
)


def _reject_leading_digit():
    from madhu.schemas.payloads import FunctionSpec
    bad = {**_VALID_SPEC, "function_name": "1reverse",
           "signature": "def 1reverse(s: str) -> str"}
    FunctionSpec(**bad)


expect_raises(
    "function_name='1reverse' (leading digit) is rejected",
    Exception, _reject_leading_digit,
)


def _reject_hyphen():
    from madhu.schemas.payloads import FunctionSpec
    bad = {**_VALID_SPEC, "function_name": "reverse-string",
           "signature": "def reverse-string(s: str) -> str"}
    FunctionSpec(**bad)


expect_raises(
    "function_name='reverse-string' (hyphen) is rejected",
    Exception, _reject_hyphen,
)


def _reject_sig_mismatch():
    from madhu.schemas.payloads import FunctionSpec
    bad = {**_VALID_SPEC, "signature": "def totally_different(s: str) -> str"}
    FunctionSpec(**bad)


expect_raises(
    "signature not containing function_name is rejected",
    Exception, _reject_sig_mismatch,
)


def _reject_empty_examples():
    from madhu.schemas.payloads import FunctionSpec
    bad = {**_VALID_SPEC, "examples": []}
    FunctionSpec(**bad)


expect_raises(
    "examples=[] (empty list) is rejected",
    Exception, _reject_empty_examples,
)


def _reject_string_constraints():
    from madhu.schemas.payloads import FunctionSpec
    bad = {**_VALID_SPEC, "constraints": "single string instead of list"}
    FunctionSpec(**bad)


expect_raises(
    "constraints='single string' (wrong type) is rejected",
    Exception, _reject_string_constraints,
)


# ===========================================================================
# §4 — Migrations
# ===========================================================================

section("§4 — Migrations")


def _migrate_0_9_to_1_0():
    from madhu.schemas.migrations import migrate
    ticket = {
        "envelope": {
            "schema_version": "0.9",
            "tier_name": "Hamsa",
            "tier_level": 24,
        },
        "payload": {"type": "function_spec"},
    }
    result = migrate(ticket)
    version = result["envelope"]["schema_version"]
    assert version == "1.0", f"Expected '1.0' after migration, got {version!r}"
    return f"0.9 → {version}"


check("migrate() upgrades a 0.9 ticket to 1.0 without error", _migrate_0_9_to_1_0)


def _migrate_1_0_idempotent():
    from madhu.schemas.migrations import migrate, CURRENT_VERSION
    ticket = {
        "envelope": {
            "schema_version": "1.0",
            "tier_name": "Hamsa",
        },
        "payload": {},
    }
    result = migrate(ticket)
    version = result["envelope"]["schema_version"]
    assert version == CURRENT_VERSION, \
        f"Expected {CURRENT_VERSION!r}, got {version!r}"
    return f"1.0 → {version} (no-op confirmed)"


check("migrate() on a 1.0 ticket is idempotent", _migrate_1_0_idempotent)


# ===========================================================================
# §5 — Naming service
# ===========================================================================

section("§5 — Naming service")


def _naming_generates_8_unique():
    from madhu.naming import NamingService
    from madhu.store.sqlite import TicketStore
    from madhu.names import RISHIS

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        store = TicketStore(tmp_path)
        svc   = NamingService(store=store)

        names = []
        for _ in range(8):
            name = svc.generate("Hamsa")
            names.append(name)
            # Mark each name as in-use by inserting a real ticket row
            # so subsequent calls see it as taken.
            with store._lock:
                store._conn.execute(
                    """INSERT INTO tickets (
                        id, schema_version, tier_name, tier_level, status,
                        collaboration_mode, mtap, created_at, updated_at,
                        created_by_agent, assigned_to_agent,
                        payload_json, failure_notes_json
                    ) VALUES (?, '1.0', 'Hamsa', 24, 'in_progress', 'solo', 1,
                              '2024-01-01T00:00:00+00:00',
                              '2024-01-01T00:00:00+00:00',
                              'param-aatma', ?, '{}', '[]')""",
                    (f"t-smoke-{len(names)}", name),
                )
                store._conn.commit()

        store.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Validate
    rishis_lower = [r.lower() for r in RISHIS]
    assert len(names) == 8,             f"Expected 8 names, got {len(names)}"
    assert len(set(names)) == 8,        f"Names not unique: {names}"
    assert all(n == n.lower() for n in names), \
        f"Non-lowercase leaf name found: {names}"
    assert all(n in rishis_lower for n in names), \
        f"Name outside RISHIS pool: {[n for n in names if n not in rishis_lower]}"

    return f"8 unique lowercase Rishi names: {names}"


check(
    "NamingService generates 8 unique lowercase names from RISHIS pool",
    _naming_generates_8_unique,
)


def _naming_exhaustion_raises():
    from madhu.naming import NamingService, NamingExhausted
    from madhu.names import RISHIS

    # Mock store that reports all RISHIS as in use
    mock_store = MagicMock()
    all_tickets = []
    for name in RISHIS:
        t = MagicMock()
        t.envelope.assigned_to_agent = name.lower()
        all_tickets.append(t)
    mock_store.list.return_value = all_tickets

    svc = NamingService(store=mock_store)
    try:
        svc.generate("Hamsa")
        raise AssertionError("Expected NamingExhausted but nothing raised")
    except NamingExhausted:
        pass  # expected


check(
    "NamingService raises NamingExhausted when all pool names are in use",
    _naming_exhaustion_raises,
)


# ===========================================================================
# §6 — SQLite schema
# ===========================================================================

section("§6 — SQLite schema")


def _schema_tables_exist():
    from madhu.store.sqlite import TicketStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        store = TicketStore(tmp_path)
        conn  = sqlite3.connect(tmp_path)
        cur   = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}

        assert "tickets"       in tables, f"'tickets' table missing. Found: {tables}"
        assert "touch_history" in tables, f"'touch_history' table missing. Found: {tables}"

        conn.close()
        store.close()
        return f"tables found: {sorted(tables)}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)



check("Tables 'tickets' and 'touch_history' exist after init", _schema_tables_exist)


def _schema_indexes_exist():
    from madhu.store.sqlite import TicketStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        store = TicketStore(tmp_path)
        conn  = sqlite3.connect(tmp_path)
        cur   = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='index';")
        indexes = {row[0] for row in cur.fetchall()}

        expected = {
            "idx_tickets_status",
            "idx_tickets_tier",
            "idx_tickets_assigned",
        }
        missing = expected - indexes
        assert not missing, f"Missing indexes: {missing}"

        conn.close()
        store.close()
        return f"indexes found: {sorted(indexes)}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)



check("All 3 indexes exist after init", _schema_indexes_exist)


def _all_statuses_insertable():
    """
    INSERT one row per status value into a real SQLite DB.
    Confirms the schema has no CHECK constraint that would reject 'aborted'
    or any other valid status.
    """
    from madhu.store.sqlite import TicketStore

    statuses = [
        "queued", "touched", "in_progress", "done",
        "failed", "killed", "forwarded", "aborted",
    ]

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        store = TicketStore(tmp_path)
        for i, status in enumerate(statuses):
            with store._lock:
                store._conn.execute(
                    """INSERT INTO tickets (
                        id, schema_version, tier_name, tier_level, status,
                        collaboration_mode, mtap, created_at, updated_at,
                        created_by_agent, payload_json, failure_notes_json
                    ) VALUES (?, '1.0', 'Hamsa', 24, ?, 'solo', 1,
                              '2024-01-01T00:00:00+00:00',
                              '2024-01-01T00:00:00+00:00',
                              'param-aatma', '{}', '[]')""",
                    (f"t-status-{i}", status),
                )
                store._conn.commit()
        store.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return f"all {len(statuses)} statuses inserted successfully"


check(
    "All 8 status values are insertable (no CHECK constraint blocks 'aborted')",
    _all_statuses_insertable,
)


def _schema_init_idempotent():
    from madhu.store.sqlite import TicketStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        store = TicketStore(tmp_path)
        store.init_schema()   # second call
        store.init_schema()   # third call
        store.close()

        # Re-open the same file — this is the real idempotency test
        store2 = TicketStore(tmp_path)
        store2.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return "init_schema() called 3x + file re-opened — no errors"


check("init_schema() is idempotent across calls and re-opens", _schema_init_idempotent)


# ===========================================================================
# §7 — Summary
# ===========================================================================

section("§7 — Summary")

total = _passed + _failed
print(f"\n  Checks run:    {total}")
print(f"  Passed:        {_passed}")
print(f"  Failed:        {_failed}")

if _failed == 0:
    print("\n  ✓ All checks passed. Ontology layer is healthy.\n")
    sys.exit(0)
else:
    print(f"\n  ✗ {_failed} check(s) failed. See output above.\n")
    sys.exit(1)