# tests/test_tiers.py
from __future__ import annotations

"""
Tests for madhu/tiers/registry.py — TierConfig and TierRegistry.

Covers:
- TierConfig: valid hamsa config parses cleanly
- TierConfig: valid adi_purusha config parses cleanly
- TierConfig: unknown provider rejected at parse time
- TierConfig: missing tier_name rejected
- TierConfig: None provider accepted (Adi Purusha has no provider)
- TierRegistry: loads both real YAML configs from madhu/tiers/configs/
- TierRegistry: missing directory raises FileNotFoundError
- TierRegistry: malformed YAML raises yaml.YAMLError
- TierRegistry: empty directory → empty _tiers, no error
- TierRegistry.get(): returns correct TierConfig for "Hamsa"
- TierRegistry.get(): unknown name raises KeyError
- TierRegistry.list_active(): sorted by tier_level ascending
- TierRegistry.deepest_active_tier(): returns "Hamsa" for v0 setup
- TierRegistry.deepest_active_tier(): raises ValueError if no tiers loaded

Does NOT cover:
- Stage 11 (scheduler): registry wired into scheduler/naming service
- Stage 9 wiring: HamsaWorker reading provider_config from registry
- provider_config key validation (unvalidated by design in v0)
"""

import tempfile
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from madhu.tiers.registry import FailurePolicy, TierConfig, TierRegistry

# ---------------------------------------------------------------------------
# Real configs directory (used for integration-style load tests)
# ---------------------------------------------------------------------------

REAL_CONFIGS_DIR = Path(__file__).parent.parent / "madhu" / "tiers" / "configs"

# ---------------------------------------------------------------------------
# Fixture dicts — mirrors what yaml.safe_load produces from the YAML files
# ---------------------------------------------------------------------------

HAMSA_DICT = {
    "tier_name": "Hamsa",
    "tier_level": 2,
    "default_agent_name": None,
    "pool": "RISHIS",
    "accepts_external": False,
    "mtap": True,
    "max_parallel": 2,
    "allowed_payload_types": ["function_spec"],
    "worker_module": "madhu.workers.hamsa",
    "worker_entrypoint": "run_worker",
    "provider": "ollama",
    "provider_config": {
        "model": "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0",
        "endpoint": "http://localhost:11434",
        "temperature": 0.2,
        "timeout": 120,
    },
    "failure_policy": {"max_forwards": 3, "on_max": "abort"},
}

ADI_PURUSHA_DICT = {
    "tier_name": "Adi Purusha",
    "tier_level": 1,
    "default_agent_name": "param-aatma",
    "accepts_external": True,
    "mtap": False,
    "max_parallel": 1,
    "allowed_payload_types": [],
    "failure_policy": {"max_forwards": 0},
}


# ---------------------------------------------------------------------------
# TierConfig — unit tests
# ---------------------------------------------------------------------------

def test_tierconfig_valid_hamsa():
    """Valid hamsa dict parses into TierConfig with correct fields."""
    config = TierConfig(**HAMSA_DICT)
    assert config.tier_name == "Hamsa"
    assert config.tier_level == 2
    assert config.provider == "ollama"
    assert config.mtap is True
    assert config.max_parallel == 2
    assert "function_spec" in config.allowed_payload_types
    assert config.failure_policy.max_forwards == 3


def test_tierconfig_valid_adi_purusha():
    """Valid adi_purusha dict parses with no provider."""
    config = TierConfig(**ADI_PURUSHA_DICT)
    assert config.tier_name == "Adi Purusha"
    assert config.tier_level == 1
    assert config.provider is None
    assert config.default_agent_name == "param-aatma"
    assert config.accepts_external is True


def test_tierconfig_none_provider_accepted():
    """provider=None is valid — Adi Purusha has no provider."""
    data = {**ADI_PURUSHA_DICT, "provider": None}
    config = TierConfig(**data)
    assert config.provider is None


def test_tierconfig_unknown_provider_rejected():
    """Unknown provider name raises ValidationError at parse time."""
    data = {**HAMSA_DICT, "provider": "vllm"}
    with pytest.raises(ValidationError, match="not registered"):
        TierConfig(**data)


def test_tierconfig_missing_tier_name_rejected():
    """Missing tier_name raises ValidationError."""
    data = {k: v for k, v in HAMSA_DICT.items() if k != "tier_name"}
    with pytest.raises(ValidationError):
        TierConfig(**data)


def test_tierconfig_failure_policy_defaults():
    """FailurePolicy defaults apply when not specified."""
    config = TierConfig(tier_name="Test", tier_level=99)
    assert config.failure_policy.max_forwards == 3
    assert config.failure_policy.on_max == "abort"


# ---------------------------------------------------------------------------
# TierRegistry — load tests (real YAML files)
# ---------------------------------------------------------------------------

def test_registry_loads_both_tiers():
    """Both adi_purusha and hamsa configs load from real YAML files."""
    registry = TierRegistry(REAL_CONFIGS_DIR)
    tiers = registry.list_active()
    names = [t.tier_name for t in tiers]
    assert "Adi Purusha" in names
    assert "Hamsa" in names


def test_registry_missing_dir_raises():
    """Non-existent configs directory raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        TierRegistry("/no/such/directory/exists")


def test_registry_invalid_yaml_raises(tmp_path):
    """Malformed YAML file raises yaml.YAMLError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("tier_name: [unclosed bracket\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        TierRegistry(tmp_path)


def test_registry_empty_directory_no_error(tmp_path):
    """Empty configs directory → empty _tiers dict, no error at init."""
    registry = TierRegistry(tmp_path)
    assert registry.list_active() == []


# ---------------------------------------------------------------------------
# TierRegistry.get()
# ---------------------------------------------------------------------------

def test_registry_get_hamsa():
    """get('Hamsa') returns the correct TierConfig."""
    registry = TierRegistry(REAL_CONFIGS_DIR)
    config = registry.get("Hamsa")
    assert config.tier_name == "Hamsa"
    assert config.provider == "ollama"


def test_registry_get_missing_raises():
    """get() raises KeyError for unknown tier name."""
    registry = TierRegistry(REAL_CONFIGS_DIR)
    with pytest.raises(KeyError, match="not loaded"):
        registry.get("Vamana")


# ---------------------------------------------------------------------------
# TierRegistry.list_active()
# ---------------------------------------------------------------------------

def test_list_active_sorted_by_level():
    """list_active() returns tiers sorted by tier_level ascending."""
    registry = TierRegistry(REAL_CONFIGS_DIR)
    tiers = registry.list_active()
    levels = [t.tier_level for t in tiers]
    assert levels == sorted(levels)
    assert tiers[0].tier_name == "Adi Purusha"
    assert tiers[-1].tier_name == "Hamsa"


# ---------------------------------------------------------------------------
# TierRegistry.deepest_active_tier()
# ---------------------------------------------------------------------------

def test_deepest_active_tier_returns_hamsa():
    """With adi_purusha + hamsa, deepest is Hamsa (tier_level=2)."""
    registry = TierRegistry(REAL_CONFIGS_DIR)
    assert registry.deepest_active_tier() == "Hamsa"


def test_deepest_active_tier_empty_raises():
    """No tiers loaded → deepest_active_tier() raises ValueError."""
    registry = TierRegistry.__new__(TierRegistry)
    registry._tiers = {}
    with pytest.raises(ValueError, match="No tiers loaded"):
        registry.deepest_active_tier()
