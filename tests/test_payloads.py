# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLAights Reserved. See LICENSE.
# tests/test_payloads.py
"""
Tests for FunctionSpec payload validation.
Covers: valid round-trip, function_name rules, signature containment,
        examples non-empty, constraints as list.
"""

import pytest
from pydantic import ValidationError

from madhu.schemas.payloads import FunctionSpec, FunctionExample


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def minimal_valid_spec(**overrides) -> dict:
    """
    Returns the minimum valid FunctionSpec dict.
    Pass overrides to test specific field variations.
    """
    base = {
        "function_name": "add",
        "signature": "def add(a: int, b: int) -> int",
        "docstring": "Return the sum of a and b.",
        "constraints": ["must handle negative numbers"],
        "examples": [{"input": "add(1, 2)", "output": "3"}],
        "imports_allowed": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Valid spec
# ---------------------------------------------------------------------------


def test_valid_function_spec_round_trip():
    """A fully valid FunctionSpec serialises and deserialises cleanly."""
    spec = FunctionSpec(**minimal_valid_spec())
    raw = spec.model_dump_json()
    restored = FunctionSpec.model_validate_json(raw)

    assert restored.function_name == "add"
    assert restored.type == "function_spec"
    assert restored.schema_version == "1.0"
    assert len(restored.examples) == 1


def test_type_and_schema_version_defaults():
    """type and schema_version are set correctly without being passed."""
    spec = FunctionSpec(**minimal_valid_spec())
    assert spec.type == "function_spec"
    assert spec.schema_version == "1.0"


def test_imports_allowed_defaults_to_empty_list():
    """imports_allowed is optional and defaults to an empty list."""
    spec = FunctionSpec(**minimal_valid_spec())
    assert spec.imports_allowed == []


def test_multiple_examples_accepted():
    """More than one example is valid."""
    spec = FunctionSpec(**minimal_valid_spec(examples=[
        {"input": "add(1, 2)", "output": "3"},
        {"input": "add(-1, 1)", "output": "0"},
    ]))
    assert len(spec.examples) == 2


# ---------------------------------------------------------------------------
# function_name validation
# ---------------------------------------------------------------------------


def test_function_name_uppercase_rejected():
    """PascalCase and camelCase names must be rejected."""
    with pytest.raises(ValidationError, match="snake_case"):
        FunctionSpec(**minimal_valid_spec(
            function_name="MyFunc",
            signature="def MyFunc(x: int) -> int",
        ))


def test_function_name_leading_digit_rejected():
    """Names starting with a digit must be rejected."""
    with pytest.raises(ValidationError, match="snake_case"):
        FunctionSpec(**minimal_valid_spec(
            function_name="1func",
            signature="def 1func(x: int) -> int",
        ))


def test_function_name_hyphen_rejected():
    """Hyphens are not valid in Python identifiers and must be rejected."""
    with pytest.raises(ValidationError, match="snake_case"):
        FunctionSpec(**minimal_valid_spec(
            function_name="my-func",
            signature="def my-func(x: int) -> int",
        ))


def test_function_name_camel_case_rejected():
    """camelCase must be rejected — only snake_case is valid."""
    with pytest.raises(ValidationError, match="snake_case"):
        FunctionSpec(**minimal_valid_spec(
            function_name="myFunc",
            signature="def myFunc(x: int) -> int",
        ))


def test_function_name_underscore_prefix_accepted():
    """Leading underscores are valid Python identifiers."""
    spec = FunctionSpec(**minimal_valid_spec(
        function_name="_helper",
        signature="def _helper(x: int) -> int",
    ))
    assert spec.function_name == "_helper"


# ---------------------------------------------------------------------------
# signature validation
# ---------------------------------------------------------------------------


def test_signature_not_containing_function_name_rejected():
    """A signature that doesn't reference the function_name must be rejected."""
    with pytest.raises(ValidationError, match="signature must contain function_name"):
        FunctionSpec(**minimal_valid_spec(
            function_name="add",
            signature="def subtract(a: int, b: int) -> int",
        ))


# ---------------------------------------------------------------------------
# examples validation
# ---------------------------------------------------------------------------


def test_empty_examples_rejected():
    """An empty examples list must be rejected."""
    with pytest.raises(ValidationError, match="at least one"):
        FunctionSpec(**minimal_valid_spec(examples=[]))


# ---------------------------------------------------------------------------
# constraints validation
# ---------------------------------------------------------------------------


def test_constraints_as_bare_string_rejected():
    """A bare string for constraints must be rejected with a clear message."""
    with pytest.raises(ValidationError, match="list of strings"):
        FunctionSpec(**minimal_valid_spec(
            constraints="must handle negative numbers",
        ))


def test_constraints_as_empty_list_accepted():
    """An empty constraints list is valid — no hard rules is a valid state."""
    spec = FunctionSpec(**minimal_valid_spec(constraints=[]))
    assert spec.constraints == []