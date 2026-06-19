# tests/test_mcp_helpers.py
"""
Tests for madhu.mcp_helpers.

Covers:
  validate_and_build_ticket — input validation, server-side default injection,
                               immutability of caller dicts
  poll_until_terminal       — polling loop, all terminal states, timeout,
                               ticket-disappeared error

Does NOT cover (tested via Stage 18 / C4 integration smoke tests):
  submit_ticket / list_tickets / check_ticket async tool handler wrappers
  scheduler interaction with submit_ticket
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from madhu.mcp_helpers import TERMINAL_STATUSES, poll_until_terminal, validate_and_build_ticket
from madhu.schemas.envelope import Ticket


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _valid_envelope() -> dict:
    return {"tier_name": "Hamsa", "tier_level": 2}


def _valid_payload() -> dict:
    return {
        "type": "function_spec",
        "function_name": "reverse_string",
        "signature": "def reverse_string(s: str) -> str:",
        "docstring": "Returns the input string reversed.",
        "constraints": [],
        "examples": [{"input": "hello", "output": "olleh"}],
        "imports_allowed": [],
    }


def _mock_ticket(status: str = "queued") -> MagicMock:
    """Return a MagicMock with the shape poll_until_terminal inspects.

    spec=Ticket is intentionally omitted: Pydantic v2 models expose fields via
    __fields__/model_fields rather than as plain class attributes, so MagicMock's
    spec machinery sets _mock_methods to the model's internal API and then blocks
    access to instance fields like `envelope`. Building the structure explicitly
    is both simpler and correct for what poll_until_terminal actually inspects.
    """
    t = MagicMock()
    t.envelope = MagicMock()
    t.envelope.status = status
    t.envelope.id = str(uuid.uuid4())
    return t


# ---------------------------------------------------------------------------
# validate_and_build_ticket
# ---------------------------------------------------------------------------

class TestValidateAndBuildTicket:
    def test_returns_ticket_instance(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert isinstance(ticket, Ticket)

    def test_server_sets_status_queued(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert ticket.envelope.status == "queued"

    def test_server_sets_created_by_agent(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert ticket.envelope.created_by_agent == "param-aatma"

    def test_server_sets_fresh_id(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        # Must be a valid UUID4
        parsed = uuid.UUID(ticket.envelope.id)
        assert parsed.version == 4

    def test_server_sets_empty_failure_notes(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert ticket.envelope.failure_notes == []

    def test_server_sets_empty_touch_history(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert ticket.envelope.touch_history == []

    def test_server_overrides_caller_status(self):
        envelope = {**_valid_envelope(), "status": "done"}
        ticket = validate_and_build_ticket(envelope, _valid_payload())
        assert ticket.envelope.status == "queued"

    def test_server_overrides_caller_created_by_agent(self):
        envelope = {**_valid_envelope(), "created_by_agent": "evil-opus"}
        ticket = validate_and_build_ticket(envelope, _valid_payload())
        assert ticket.envelope.created_by_agent == "param-aatma"

    def test_server_overrides_caller_id(self):
        old_id = str(uuid.uuid4())
        envelope = {**_valid_envelope(), "id": old_id}
        ticket = validate_and_build_ticket(envelope, _valid_payload())
        assert ticket.envelope.id != old_id

    def test_does_not_mutate_caller_envelope(self):
        caller_envelope = _valid_envelope()
        keys_before = dict(caller_envelope)
        validate_and_build_ticket(caller_envelope, _valid_payload())
        assert caller_envelope == keys_before

    def test_does_not_mutate_caller_payload(self):
        caller_payload = _valid_payload()
        keys_before = dict(caller_payload)
        validate_and_build_ticket(_valid_envelope(), caller_payload)
        assert caller_payload == keys_before

    def test_unknown_payload_type_raises_value_error(self):
        payload = {**_valid_payload(), "type": "task_brief"}
        with pytest.raises(ValueError, match="unsupported payload type"):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_missing_payload_type_raises_value_error(self):
        payload = {k: v for k, v in _valid_payload().items() if k != "type"}
        with pytest.raises(ValueError, match="unsupported payload type"):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_bad_function_name_raises_validation_error(self):
        payload = {**_valid_payload(), "function_name": "BadName"}
        with pytest.raises(ValidationError):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_function_name_with_leading_digit_raises(self):
        payload = {**_valid_payload(), "function_name": "1bad"}
        with pytest.raises(ValidationError):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_function_name_with_hyphen_raises(self):
        payload = {**_valid_payload(), "function_name": "bad-name"}
        with pytest.raises(ValidationError):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_empty_examples_raises_validation_error(self):
        payload = {**_valid_payload(), "examples": []}
        with pytest.raises(ValidationError):
            validate_and_build_ticket(_valid_envelope(), payload)

    def test_schema_version_defaulted_to_1_0(self):
        ticket = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert ticket.envelope.schema_version == "1.0"

    def test_two_tickets_have_distinct_ids(self):
        t1 = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        t2 = validate_and_build_ticket(_valid_envelope(), _valid_payload())
        assert t1.envelope.id != t2.envelope.id


# ---------------------------------------------------------------------------
# poll_until_terminal
# ---------------------------------------------------------------------------

class TestPollUntilTerminal:

    def _store_returning_sequence(self, statuses: list[str]) -> MagicMock:
        """Mock store whose read() returns tickets cycling through statuses.
        Last status is repeated once exhausted."""
        store = MagicMock()
        call_count = [0]

        def _read(_ticket_id: str):
            idx = min(call_count[0], len(statuses) - 1)
            call_count[0] += 1
            return _mock_ticket(statuses[idx])

        store.read.side_effect = _read
        return store

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
    def test_resolves_immediately_on_terminal_status(self, terminal: str):
        store = self._store_returning_sequence([terminal])
        ticket = poll_until_terminal(store, "t-001", timeout=5.0)
        assert ticket.envelope.status == terminal

    def test_resolves_after_non_terminal_calls(self):
        store = self._store_returning_sequence(["queued", "in_progress", "done"])
        with patch("madhu.mcp_helpers.time.sleep"):
            ticket = poll_until_terminal(store, "t-002", timeout=5.0)
        assert ticket.envelope.status == "done"
        assert store.read.call_count == 3

    def test_timeout_returns_current_state_no_exception(self):
        store = MagicMock()
        store.read.return_value = _mock_ticket("queued")
        ticket = poll_until_terminal(store, "t-003", timeout=0.1)
        assert ticket.envelope.status == "queued"

    def test_timeout_does_not_kill_or_modify_ticket(self):
        store = MagicMock()
        store.read.return_value = _mock_ticket("queued")
        poll_until_terminal(store, "t-004", timeout=0.05)
        store.create.assert_not_called()
        store.update.assert_not_called()

    def test_timeout_completes_near_deadline(self):
        store = MagicMock()
        store.read.return_value = _mock_ticket("in_progress")
        start = time.monotonic()
        poll_until_terminal(store, "t-005", timeout=0.15)
        elapsed = time.monotonic() - start
        # Should exit within a generous margin of the timeout, not hang.
        assert elapsed < 2.0

    def test_ticket_disappeared_raises_runtime_error(self):
        store = MagicMock()
        store.read.return_value = None
        with pytest.raises(RuntimeError, match="disappeared"):
            poll_until_terminal(store, "t-missing", timeout=5.0)

    def test_all_terminal_statuses_are_recognized(self):
        """Sanity-check that TERMINAL_STATUSES matches the status enum."""
        expected = {"done", "failed", "killed", "aborted"}
        assert TERMINAL_STATUSES == expected
