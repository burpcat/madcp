# madhu/schemas/payloads.py
"""
Tier-specific payload schemas for MadCP — madhu.

Each payload type is a Pydantic v2 model. The Ticket envelope carries
the payload as a raw dict — payload validation is performed by the
worker that receives it, not by the store or scheduler.

Current payload types:
  function_spec  — Hamsa tier, used by the Gemma worker (stage 9)

Future payload types (not yet implemented):
  task_brief     — planner tiers, when intermediate tiers are activated
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ---------------------------------------------------------------------------
# Input/output example pair
# ---------------------------------------------------------------------------


class FunctionExample(BaseModel):
    """
    One concrete input/output pair for the function being specified.

    These serve two purposes: they guide Gemma's generation (the worker
    includes them in the prompt as expected behaviour), and they become
    the basis for the ast-level smoke test after generation.

    'input' and 'output' are strings rather than typed values because
    the payload layer doesn't know the function's argument types — that's
    encoded in the signature and docstring.
    """
    model_config = ConfigDict(use_enum_values=True)

    input:  str   # e.g. "parse_query_string('a=1&b=2')"
    output: str   # e.g. "{'a': '1', 'b': '2'}"


# ---------------------------------------------------------------------------
# FunctionSpec — Hamsa tier payload
# ---------------------------------------------------------------------------


class FunctionSpec(BaseModel):
    """
    Specifies a single Python function for the Gemma worker to implement.

    This is the only payload type consumed by the Hamsa tier in v0.
    The MCP surface (stage 12) validates incoming payloads against this
    schema before inserting the ticket into the store — a malformed spec
    is rejected at submission time, not discovered mid-worker-cycle.

    Field rules (enforced by validators below):
      - function_name  must be snake_case: ^[a-z_][a-z0-9_]*$
      - signature      must contain function_name verbatim
      - examples       must be non-empty (at least one required)
      - constraints    must be a list, not a bare string
    """
    model_config = ConfigDict(use_enum_values=True)

    # Discriminator fields — used by the store and migrations framework
    type:           Literal["function_spec"] = "function_spec"
    schema_version: str = "1.0"

    # Core spec fields
    function_name:   str
    signature:       str              # full Python signature e.g. "def add(a: int, b: int) -> int"
    docstring:       str              # describes intent, args, return value, edge cases
    constraints:     list[str]        # hard rules Gemma must not violate
    examples:        list[FunctionExample]   # at least one required
    imports_allowed: list[str] = []   # empty = no imports permitted

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("function_name")
    @classmethod
    def function_name_must_be_snake_case(cls, v: str) -> str:
        """
        Enforces snake_case: lowercase letters, digits (not at start),
        underscores only. Rejects: uppercase letters, hyphens, leading
        digits, camelCase, PascalCase.
        Examples rejected: MyFunc, 1func, my-func, myFunc
        Examples accepted: my_func, parse_query_string, _helper
        """
        pattern = r"^[a-z_][a-z0-9_]*$"
        if not re.match(pattern, v):
            raise ValueError(
                f"function_name {v!r} must be snake_case: "
                f"start with a lowercase letter or underscore, "
                f"contain only lowercase letters, digits, and underscores."
            )
        return v

    @field_validator("examples")
    @classmethod
    def examples_must_be_non_empty(cls, v: list[FunctionExample]) -> list[FunctionExample]:
        """
        At least one example is required. A spec with no examples gives
        the Gemma worker no concrete behaviour to target and no basis
        for the post-generation smoke test.
        """
        if not v:
            raise ValueError(
                "examples must contain at least one input/output pair. "
                "The Gemma worker uses these to verify its output."
            )
        return v

    @field_validator("constraints", mode="before")
    @classmethod
    def constraints_must_be_a_list(cls, v: object) -> object:
        """
        Guards against a common mistake: passing constraints as a single
        string instead of a list. A bare string is silently iterable in
        Python, so without this check it would pass through as a list of
        characters — a hard-to-debug failure mode.
        """
        if isinstance(v, str):
            raise ValueError(
                "constraints must be a list of strings, not a bare string. "
                "Wrap it: constraints=['your constraint here']"
            )
        return v

    @model_validator(mode="after")
    def signature_must_contain_function_name(self) -> "FunctionSpec":
        """
        The signature must reference the function_name — otherwise the
        worker would generate a function with a different name than the
        spec requested, and the result would be silently wrong.
        Runs after all field validators so both fields are already clean.
        """
        if self.function_name not in self.signature:
            raise ValueError(
                f"signature must contain function_name {self.function_name!r}. "
                f"Got signature: {self.signature!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Payload union — extend as new tiers are added
# ---------------------------------------------------------------------------

# When a task_brief payload is added for planner tiers, add it here.
# The store and MCP surface can use this union for dispatch.
AnyPayload = FunctionSpec