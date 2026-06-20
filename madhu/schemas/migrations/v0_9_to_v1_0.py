# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/schemas/migrations/v0_9_to_v1_0.py
"""
Migration: schema version 0.9 → 1.0

This is a no-op stub. It exists to:
  1. Prove the migrations framework dispatches correctly.
  2. Provide a template for real future migrations.
  3. Ensure test coverage of the migrate-on-read path from day one.

In a real migration, this function would reshape the dict —
renaming fields, adding defaults for new required fields,
or dropping obsolete ones.
"""

from __future__ import annotations


def upgrade(ticket_dict: dict) -> dict:
    """
    Upgrade a ticket dict from schema version 0.9 to 1.0.

    Currently a no-op: stamps the new version and returns the dict
    unchanged. A real migration would transform fields here.
    """
    # Stamp the new version in whichever location it lives.
    if "envelope" in ticket_dict and isinstance(ticket_dict["envelope"], dict):
        ticket_dict["envelope"]["schema_version"] = "1.0"
    else:
        ticket_dict["schema_version"] = "1.0"

    return ticket_dict