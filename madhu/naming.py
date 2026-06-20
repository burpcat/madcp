# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
# madhu/naming.py
"""
Naming service for MadCP — madhu.

Generates unique agent names for workers at a given tier, drawn from
that tier's assigned name pool (defined in madhu/names.py).

Pool assignment (v0):
  Hamsa (leaf) → RISHIS   — 8 names, lowercase at generation time

When intermediate tiers are activated (Phase 2), extend TIER_POOL_MAP
below and add the tier's worker_pool field to its YAML config (stage 10).
At that point, the tier YAML becomes the source of truth for pool
assignment and TIER_POOL_MAP is removed.

Leaf-tier rule:
  The deepest currently active tier produces lowercase names.
  For v0 this is always Hamsa. The naming service receives the list of
  active tiers from the caller (scheduler) to determine which is deepest.
  If active_tiers is not provided, Hamsa is assumed to be the leaf.

Collision behaviour:
  Names are unique within a tier at any given moment (not globally).
  A name is considered in-use if any ticket in that tier has
  status in (queued, touched, in_progress) and assigned_to_agent
  matches the name.
  On collision: regenerate, up to MAX_ATTEMPTS times.
  On exhaustion: raise NamingExhausted.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from madhu.names import RISHIS, HEROES, GRAHA, GUARDIANS, PEETHAS, VAHANAS, KRISHNAS

if TYPE_CHECKING:
    from madhu.store.sqlite import TicketStore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NamingExhausted(Exception):
    """
    Raised when all names in the pool for a tier are currently in use
    and no unique name can be generated within MAX_ATTEMPTS retries.
    With max_parallel=2 and RISHIS having 8 entries this should never
    fire in v0 — it is a safety net, not an expected code path.
    """
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ATTEMPTS = 10

# Active statuses that mean a name is currently in use.
# A name is free once its ticket reaches a terminal state.
ACTIVE_STATUSES = {"queued", "touched", "in_progress"}

# Pool assignment map: tier_name → name pool.
# This is the v0 interim home for this mapping.
# Stage 10 (tier registry) will move this into the tier YAML configs.
# When that happens, the NamingService will accept the pool as a
# parameter from the tier config rather than looking it up here.
TIER_POOL_MAP: dict[str, list[str]] = {
    "Hamsa":       RISHIS,
    # Future intermediate tiers — uncomment and assign when activated:
    # "Kalki":     HEROES,
    # "Buddha":    GRAHA,
    # "Krishna":   GUARDIANS,
    # "Balarama":  PEETHAS,
    # "Rama":      VAHANAS,
}

# The leaf tier for v0. Used to determine whether to lowercase names.
# Stage 11 (scheduler) will pass active_tiers dynamically instead.
DEFAULT_LEAF_TIER = "Hamsa"


# ---------------------------------------------------------------------------
# Naming service
# ---------------------------------------------------------------------------


class NamingService:
    """
    Generates unique agent names for workers at a given tier.

    Args:
        store: A TicketStore instance used to check which names are
               currently in use. The naming service never writes to
               the store — read-only access only.
        leaf_tier: The name of the deepest currently active tier.
                   Workers at this tier get lowercase names.
                   Defaults to DEFAULT_LEAF_TIER ("Hamsa") for v0.
    """

    def __init__(
        self,
        store: "TicketStore",
        leaf_tier: str = DEFAULT_LEAF_TIER,
    ) -> None:
        self._store = store
        self._leaf_tier = leaf_tier

    def generate(self, tier_name: str) -> str:
        """
        Generate a unique agent name for a worker at the given tier.

        Shuffles the pool without replacement rather than sampling with
        replacement — this guarantees a free name is found in at most
        len(pool) attempts if one exists, removing the probabilistic
        failure mode of random.choice with a near-exhausted pool.

        The name is lowercased if tier_name is the current leaf tier.

        Args:
            tier_name: Must be a value from KRISHNAS and have an entry
                       in TIER_POOL_MAP.

        Returns:
            A unique agent name string, e.g. "vasishtha" (Hamsa tier).

        Raises:
            ValueError: If tier_name has no pool assigned.
            NamingExhausted: If all pool names are currently in use.
        """
        pool = self._get_pool(tier_name)
        in_use = self._get_in_use(tier_name)
        is_leaf = (tier_name == self._leaf_tier)

        # Shuffle without replacement — iterate through a randomised copy
        # of the pool. This guarantees we find a free name in at most
        # len(pool) attempts if one exists, unlike random.choice which
        # samples with replacement and can exhaust MAX_ATTEMPTS even when
        # free names remain.
        shuffled = random.sample(pool, len(pool))
        for candidate in shuffled:
            name = candidate.lower() if is_leaf else candidate
            if name not in in_use:
                return name

        raise NamingExhausted(
            f"Could not generate a unique name for tier {tier_name!r}. "
            f"All {len(pool)} names in the pool are currently in use. "
            f"Check max_parallel setting for this tier."
        )


    def _get_pool(self, tier_name: str) -> list[str]:
        """
        Return the name pool for the given tier.
        Raises ValueError if the tier has no registered pool.
        """
        pool = TIER_POOL_MAP.get(tier_name)
        if pool is None:
            raise ValueError(
                f"No name pool registered for tier {tier_name!r}. "
                f"Add it to TIER_POOL_MAP in madhu/naming.py. "
                f"Available tiers: {list(TIER_POOL_MAP.keys())}"
            )
        return pool

    def _get_in_use(self, tier_name: str) -> set[str]:
        """
        Query the store for agent names currently active in this tier.
        A name is in use if any ticket in the tier has an active status
        and assigned_to_agent set.

        # Normalise to lowercase — leaf tier names are always stored
        # lowercase because the naming service lowercases at generation.
        # The .lower() here is defensive for future non-leaf tiers that
        # might share a pool with mixed-case stored names.
        """
        active_tickets = self._store.list(
            tier=tier_name,
            status_in=ACTIVE_STATUSES,
        )
        return {
            t.envelope.assigned_to_agent.lower()
            for t in active_tickets
            if t.envelope.assigned_to_agent is not None
        }