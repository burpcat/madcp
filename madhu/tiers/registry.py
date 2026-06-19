# madhu/tiers/registry.py
from __future__ import annotations

"""
Tier registry for MadCP.

Loads and validates tier YAML configs from madhu/tiers/configs/.
One YAML file per tier. All callers get the same validated state
from a single TierRegistry instance constructed at server startup.

Cross-layer import note:
    registry.py imports PROVIDER_REGISTRY from madhu.workers.providers
    for provider name validation. The import chain is:
        tiers.registry → workers.providers → workers.base → store.sqlite/touch
    Nothing in store or workers imports from tiers — no circular dependency.

Config reload policy: load once at init. Restart server to pick up
YAML changes. No file watching in v0.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from madhu.workers.providers import PROVIDER_REGISTRY


# ---------------------------------------------------------------------------
# Pydantic schema for a single tier config
# ---------------------------------------------------------------------------

class FailurePolicy(BaseModel):
    """Failure forwarding policy for a tier."""

    model_config = ConfigDict(use_enum_values=True)

    max_forwards: int = 3
    on_max: str = "abort"


class TierConfig(BaseModel):
    """
    Validated schema for a single tier's YAML config.

    provider_config is dict[str, Any] — unvalidated at registry load time.
    Missing keys (model, temperature, timeout) fall through to HamsaWorker
    defaults. This is intentional for v0: the YAML is the source of config,
    but HamsaWorker's defaults are the safety net.

    provider field (when present) is validated against PROVIDER_REGISTRY
    at parse time — unknown providers are rejected at startup, not mid-ticket.
    """

    model_config = ConfigDict(use_enum_values=True)

    tier_name: str
    tier_level: int
    default_agent_name: str | None = None
    accepts_external: bool = False
    mtap: bool = True
    max_parallel: int = 1
    allowed_payload_types: list[str] = Field(default_factory=list)
    pool: str | None = None
    worker_module: str | None = None
    worker_entrypoint: str | None = None
    provider: str | None = None
    provider_config: dict[str, Any] = Field(default_factory=dict)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    @field_validator("provider")
    @classmethod
    def provider_must_be_registered(cls, v: str | None) -> str | None:
        """
        Validate provider name against PROVIDER_REGISTRY.

        None is allowed (Adi Purusha has no provider — it doesn't spawn workers).
        Any non-None value must be a key in PROVIDER_REGISTRY.
        """
        if v is not None and v not in PROVIDER_REGISTRY:
            raise ValueError(
                f"provider {v!r} is not registered. "
                f"Known providers: {list(PROVIDER_REGISTRY.keys())}"
            )
        return v


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TierRegistry:
    """
    Loads all tier YAML configs from a directory and exposes lookup methods.

    Usage:
        registry = TierRegistry(configs_dir="madhu/tiers/configs")
        hamsa = registry.get("Hamsa")
        leaf = registry.deepest_active_tier()

    Thread safety: read-only after __init__. Safe to share across threads.
    """

    def __init__(self, configs_dir: Path | str) -> None:
        """
        Load and validate all .yaml files in configs_dir.

        Empty directory → empty _tiers dict, no error at init time.
        ValueError is deferred to deepest_active_tier() if no tiers loaded.

        Raises:
            FileNotFoundError: if configs_dir does not exist
            yaml.YAMLError: if any .yaml file is malformed
            pydantic.ValidationError: if any config fails schema validation
        """
        self._configs_dir = Path(configs_dir)
        if not self._configs_dir.exists():
            raise FileNotFoundError(
                f"Tier configs directory not found: {self._configs_dir}"
            )

        self._tiers: dict[str, TierConfig] = {}
        for yaml_path in sorted(self._configs_dir.glob("*.yaml")):
            with yaml_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
            config = TierConfig(**data)
            self._tiers[config.tier_name] = config

    def get(self, tier_name: str) -> TierConfig:
        """
        Return config for a named tier.

        Raises:
            KeyError: if tier_name not in loaded configs
        """
        if tier_name not in self._tiers:
            raise KeyError(
                f"Tier {tier_name!r} not loaded. "
                f"Available: {list(self._tiers.keys())}"
            )
        return self._tiers[tier_name]

    def list_active(self) -> list[TierConfig]:
        """
        Return all loaded tier configs sorted by tier_level ascending.

        Returns empty list if no tiers loaded.
        """
        return sorted(self._tiers.values(), key=lambda t: t.tier_level)

    def deepest_active_tier(self) -> str:
        """
        Return the tier_name of the highest tier_level among loaded tiers.

        Used by NamingService to apply the lowercase rule to leaf workers.
        For v0 (Adi Purusha + Hamsa), returns "Hamsa".

        Note: deepest_active_tier() returns "Hamsa" for the v0 two-tier setup.
        NamingService hardcodes {"Hamsa": RISHIS} as its pool assignment —
        the strings match without registry wiring at this stage.

        Raises:
            ValueError: if no tiers are loaded
        """
        tiers = self.list_active()
        if not tiers:
            raise ValueError("No tiers loaded — cannot determine deepest tier")
        return tiers[-1].tier_name
