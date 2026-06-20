# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# tests/test_naming.py
"""
Tests for the NamingService.
Covers: name generation, pool lookup, leaf-tier lowercasing,
        collision detection, pool exhaustion.

Uses a mock store — NamingService only reads from the store,
so a simple stub returning controlled ticket lists is sufficient.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from madhu.naming import NamingService, NamingExhausted, TIER_POOL_MAP
from madhu.names import RISHIS
from madhu.schemas.envelope import Envelope, Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_store(assigned_names: list[str] | None = None) -> MagicMock:
    """
    Build a mock TicketStore whose list() method returns tickets
    with the given assigned_to_agent names, all in 'in_progress' status.

    If assigned_names is None or empty, the store returns no active tickets
    (pool is fully available).
    """
    store = MagicMock()
    tickets = []

    for name in (assigned_names or []):
        env = Envelope(
            tier_name="Hamsa",
            tier_level=24,
            status="in_progress",
            assigned_to_agent=name,
        )
        tickets.append(Ticket(envelope=env, payload={"type": "function_spec"}))

    store.list.return_value = tickets
    return store


def make_service(assigned_names: list[str] | None = None) -> NamingService:
    """Build a NamingService backed by a mock store."""
    store = make_mock_store(assigned_names)
    return NamingService(store=store, leaf_tier="Hamsa")


# ---------------------------------------------------------------------------
# Pool lookup
# ---------------------------------------------------------------------------


def test_hamsa_pool_is_rishis():
    """TIER_POOL_MAP must assign RISHIS to the Hamsa tier."""
    assert TIER_POOL_MAP["Hamsa"] is RISHIS


def test_unknown_tier_raises_value_error():
    """Requesting a name for an unregistered tier must raise ValueError."""
    service = make_service()
    with pytest.raises(ValueError, match="No name pool registered"):
        service.generate("UnknownTier")


# ---------------------------------------------------------------------------
# Name format
# ---------------------------------------------------------------------------


def test_generated_name_is_from_rishis_pool():
    """Every generated Hamsa name must come from the RISHIS pool."""
    service = make_service()
    # Run many times to cover randomness
    for _ in range(50):
        name = service.generate("Hamsa")
        assert name in [r.lower() for r in RISHIS], (
            f"Generated name {name!r} is not in the lowercased RISHIS pool"
        )


def test_hamsa_names_are_lowercase():
    """
    Hamsa is the leaf tier — all generated names must be lowercase.
    e.g. 'vasishtha', not 'Vasishtha'.
    """
    service = make_service()
    for _ in range(20):
        name = service.generate("Hamsa")
        assert name == name.lower(), f"Expected lowercase, got {name!r}"


def test_name_format_is_single_word():
    """
    Names from RISHIS are single words (no hyphens, no spaces).
    The adj-noun format from the original spec has been retired.
    """
    service = make_service()
    for _ in range(20):
        name = service.generate("Hamsa")
        assert "-" not in name
        assert " " not in name


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def test_collision_skips_in_use_names():
    """
    If a name is currently in use, generate() must not return it.
    With one name in use and 7 remaining, a unique name must be found.
    """
    # Mark 'vasishtha' as in use
    service = make_service(assigned_names=["vasishtha"])
    results = {service.generate("Hamsa") for _ in range(30)}
    assert "vasishtha" not in results


def test_multiple_collisions_resolved():
    """
    With several names in use, the service must still find a free one.
    """
    in_use = ["sanaka", "sananda", "sanatana", "vasishtha", "vishwamitra"]
    service = make_service(assigned_names=in_use)
    # 3 names remain: agastya, atri, bharadwaja
    for _ in range(20):
        name = service.generate("Hamsa")
        assert name not in in_use


# ---------------------------------------------------------------------------
# Pool exhaustion
# ---------------------------------------------------------------------------


def test_exhausted_pool_raises_naming_exhausted():
    """
    When all names in the pool are in use, NamingExhausted must be raised
    after MAX_ATTEMPTS retries.
    """
    all_names = [r.lower() for r in RISHIS]
    service = make_service(assigned_names=all_names)
    with pytest.raises(NamingExhausted, match="Could not generate"):
        service.generate("Hamsa")


def test_naming_exhausted_message_includes_tier():
    """The exhaustion error message must name the tier for debuggability."""
    all_names = [r.lower() for r in RISHIS]
    service = make_service(assigned_names=all_names)
    with pytest.raises(NamingExhausted, match="Hamsa"):
        service.generate("Hamsa")