# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/schemas/migrations/__init__.py
"""
Migrate-on-read framework for MadCP ticket schema versioning.

How it works:
  1. Every ticket dict read from SQLite passes through migrate() before
     being deserialised into a Ticket object.
  2. migrate() checks the dict's schema_version and applies registered
     upgrade functions in order until the dict reaches CURRENT_VERSION.
  3. Each migration lives in its own file: v{from}_to_{to}.py, exposing
     a single function: def upgrade(d: dict) -> dict.
  4. Migrations are append-only. Never modify an existing migration file
     after it has been applied to production data.

To add a new migration (e.g. 1.0 → 1.1):
  1. Create madhu/schemas/migrations/v1_0_to_v1_1.py with upgrade().
  2. Add ("1.0", "1.1") → v1_0_to_v1_1.upgrade to MIGRATIONS below.
  3. Update CURRENT_VERSION to "1.1".

Schema version is stored on both the envelope and the payload.
migrate() updates both.
"""

from __future__ import annotations

from madhu.schemas.migrations import v0_9_to_v1_0

# ---------------------------------------------------------------------------
# Version registry
# ---------------------------------------------------------------------------

CURRENT_VERSION = "1.0"

# Ordered list of (from_version, to_version, upgrade_function).
# Applied in sequence — a ticket at 0.9 will pass through every entry
# whose from_version matches its current version until it reaches
# CURRENT_VERSION.
MIGRATIONS: list[tuple[str, str, object]] = [
    ("0.9", "1.0", v0_9_to_v1_0.upgrade),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def migrate(ticket_dict: dict) -> dict:
    """
    Bring a ticket dict up to CURRENT_VERSION by applying any registered
    migrations in order. Returns the (possibly mutated) dict.

    Called by the store on every read. If the ticket is already at
    CURRENT_VERSION, this is a fast no-op — just a version string check.

    The dict is mutated in-place and also returned, so callers can
    use either style:
        ticket_dict = migrate(ticket_dict)   # reassign
        migrate(ticket_dict)                 # mutate in place
    """
    # Read version from the envelope sub-dict if present, else top-level.
    # This handles both raw SQLite row dicts and fully-nested ticket dicts.
    version = _get_version(ticket_dict)

    for from_ver, to_ver, upgrade_fn in MIGRATIONS:
        if version == CURRENT_VERSION:
            break
        if version == from_ver:
            ticket_dict = upgrade_fn(ticket_dict)
            # After upgrade, re-read the version — the upgrade function
            # is responsible for setting it correctly.
            version = _get_version(ticket_dict)

    return ticket_dict


def _get_version(ticket_dict: dict) -> str:
    """
    Extract schema_version from wherever it lives in the dict.
    Handles both flat dicts (raw SQLite rows) and nested dicts
    (deserialised Ticket objects).
    """
    # Nested form: {"envelope": {"schema_version": "1.0"}, ...}
    if "envelope" in ticket_dict and isinstance(ticket_dict["envelope"], dict):
        return ticket_dict["envelope"].get("schema_version", CURRENT_VERSION)
    # Flat form: {"schema_version": "1.0", ...}
    return ticket_dict.get("schema_version", CURRENT_VERSION)