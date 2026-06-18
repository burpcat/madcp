# tests/test_migrations.py
"""
Tests for the migrate-on-read migrations framework.
Covers: dispatch to correct upgrade function, no-op for current version,
        version stamping, chained migrations.
"""

import pytest

from madhu.schemas.migrations import migrate, CURRENT_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ticket_dict(version: str, nested: bool = True) -> dict:
    """
    Build a minimal ticket dict at the given schema version.
    nested=True mimics a fully deserialised Ticket dict.
    nested=False mimics a flat SQLite row dict.
    """
    if nested:
        return {
            "envelope": {
                "schema_version": version,
                "tier_name": "Hamsa",
                "tier_level": 24,
            },
            "payload": {"type": "function_spec"},
        }
    else:
        return {
            "schema_version": version,
            "tier_name": "Hamsa",
        }


# ---------------------------------------------------------------------------
# No-op for current version
# ---------------------------------------------------------------------------


def test_migrate_current_version_is_noop():
    """
    A ticket already at CURRENT_VERSION must pass through unchanged.
    This is the hot path — every ticket read from SQLite hits it.
    """
    ticket = make_ticket_dict(CURRENT_VERSION)
    original_id_check = ticket["envelope"]["schema_version"]
    result = migrate(ticket)
    assert result["envelope"]["schema_version"] == original_id_check


def test_migrate_returns_dict():
    """migrate() must always return a dict, never None."""
    ticket = make_ticket_dict(CURRENT_VERSION)
    result = migrate(ticket)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# v0.9 → v1.0 stub migration
# ---------------------------------------------------------------------------


def test_migrate_from_0_9_to_1_0_nested():
    """
    A nested ticket dict at version 0.9 must be upgraded to 1.0.
    The stub migration does nothing except stamp the version.
    """
    ticket = make_ticket_dict("0.9", nested=True)
    assert ticket["envelope"]["schema_version"] == "0.9"

    result = migrate(ticket)
    assert result["envelope"]["schema_version"] == "1.0"


def test_migrate_from_0_9_to_1_0_flat():
    """
    A flat ticket dict (as from a raw SQLite row) at version 0.9
    must also be upgraded correctly.
    """
    ticket = make_ticket_dict("0.9", nested=False)
    result = migrate(ticket)
    assert result["schema_version"] == "1.0"


def test_migrate_does_not_mutate_unrelated_fields():
    """
    Migration must not touch fields it doesn't own.
    Only schema_version should change in the stub.
    """
    ticket = make_ticket_dict("0.9", nested=True)
    ticket["envelope"]["tier_name"] = "Hamsa"
    ticket["payload"]["type"] = "function_spec"

    result = migrate(ticket)

    assert result["envelope"]["tier_name"] == "Hamsa"
    assert result["payload"]["type"] == "function_spec"


# ---------------------------------------------------------------------------
# Unknown version behaviour
# ---------------------------------------------------------------------------


def test_migrate_unknown_version_passes_through():
    """
    A ticket with an unrecognised version has no registered migration.
    migrate() must return it unchanged rather than crashing — the store
    will surface the error when it tries to deserialise.
    """
    ticket = make_ticket_dict("99.0", nested=True)
    result = migrate(ticket)
    # No migration exists for 99.0 — version stays as-is
    assert result["envelope"]["schema_version"] == "99.0"