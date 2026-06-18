from __future__ import annotations

"""
Tests for madhu/store/markdown.py — MarkdownSync.

Covers:
- sync_ticket() writes a file with correct structure
- YAML frontmatter contains required fields
- parent_id rendered as [[wiki-link]] when set
- forwarded_from rendered as [[wiki-link]] when set
- parent_id / forwarded_from are None (not wiki-links) when absent
- failure_notes ticket_id rendered as [[wiki-link]]
- touch_history section populates when entries are present
- result section populates when result is present
- delete_ticket() removes the file
- delete_ticket() is a no-op if file does not exist
- sync_all() writes one file per ticket (uses store.list() + store.read())
- _on_ticket_write wiring: store.create() triggers sync automatically

Does NOT cover:
- Stage 8 (touch protocol) — touch_history populated via touch manager
- Stage 12 (MCP server init) — wiring _on_ticket_write in server context
- Concurrent write safety — covered by store threading lock (stage 5/6)
"""

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
import yaml

from madhu.schemas.envelope import (
    Envelope,
    FailureNote,
    Result,
    Ticket,
    TicketStatus,
    TouchEntry,
)
from madhu.schemas.payloads import FunctionSpec
from madhu.store.markdown import MarkdownSync, _render_ticket, _wiki
from madhu.store.sqlite import TicketStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_envelope(**kwargs) -> Envelope:
    """Return a minimal valid Envelope, overridable via kwargs."""
    defaults = dict(
        id="test-ticket-0001",
        tier_name="Hamsa",
        tier_level=2,
        status="queued",
        created_by_agent="param-aatma",
    )
    defaults.update(kwargs)
    return Envelope(**defaults)


def make_ticket(envelope_kwargs=None, payload=None, result=None) -> Ticket:
    """Return a minimal valid Ticket."""
    env = make_envelope(**(envelope_kwargs or {}))
    if payload is None:
        payload = FunctionSpec(
        function_name="add_two",
        signature="def add_two(a: int, b: int) -> int",
        docstring="Return a + b.",
        constraints=["must handle negative numbers"],
        examples=[{"input": "a=1, b=2", "output": "3"}],
        imports_allowed=[],
        )
    return Ticket(envelope=env, payload=payload.model_dump(), result=result)


@pytest.fixture
def tmp_tickets(tmp_path) -> Path:
    """Return a temp directory to act as tickets/."""
    d = tmp_path / "tickets"
    d.mkdir()
    return d


@pytest.fixture
def sync(tmp_tickets) -> MarkdownSync:
    """Return a MarkdownSync pointed at tmp_tickets."""
    return MarkdownSync(tickets_dir=tmp_tickets)


# ---------------------------------------------------------------------------
# Unit tests — _wiki helper
# ---------------------------------------------------------------------------

def test_wiki_returns_link_for_id():
    assert _wiki("abc-123") == "[[abc-123]]"


def test_wiki_returns_none_for_none():
    assert _wiki(None) is None


# ---------------------------------------------------------------------------
# sync_ticket — file creation
# ---------------------------------------------------------------------------

def test_sync_ticket_creates_file(sync, tmp_tickets):
    """sync_ticket() creates a .md file named {id}.md."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    expected = tmp_tickets / "test-ticket-0001.md"
    assert expected.exists()


def test_sync_ticket_overwrites_existing(sync, tmp_tickets):
    """Calling sync_ticket() twice overwrites without error."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    # Modify status and sync again
    env_dict = ticket.envelope.model_dump()
    env_dict["status"] = "done"
    ticket2 = Ticket(
        envelope=Envelope(**env_dict),
        payload=ticket.payload,
        result=ticket.result,
    )
    sync.sync_ticket(ticket2)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "status: done" in content


# ---------------------------------------------------------------------------
# Frontmatter correctness
# ---------------------------------------------------------------------------

def _parse_frontmatter(md: str) -> dict:
    """Extract and parse the YAML frontmatter block from a markdown string."""
    match = re.match(r"^---\n(.*?)\n---\n", md, re.DOTALL)
    assert match, "No frontmatter found"
    return yaml.safe_load(match.group(1))


def test_frontmatter_required_fields(sync, tmp_tickets):
    """Frontmatter contains all required envelope fields."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    fm = _parse_frontmatter(content)
    for field in ("id", "schema_version", "tier_name", "tier_level", "status",
                  "created_by_agent", "mtap"):
        assert field in fm, f"Missing frontmatter field: {field}"


def test_frontmatter_id_value(sync, tmp_tickets):
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    fm = _parse_frontmatter(content)
    assert fm["id"] == "test-ticket-0001"


def test_frontmatter_created_by_agent_is_param_aatma(sync, tmp_tickets):
    """created_by_agent defaults to param-aatma — not 'madhu' (stale name)."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    fm = _parse_frontmatter(content)
    assert fm["created_by_agent"] == "param-aatma"


# ---------------------------------------------------------------------------
# Wiki-link rendering
# ---------------------------------------------------------------------------

def test_parent_id_renders_as_wiki_link(sync, tmp_tickets):
    """parent_id is rendered as [[parent-id]] in frontmatter."""
    ticket = make_ticket(envelope_kwargs={"id": "child-001", "parent_id": "parent-001"})
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "child-001.md").read_text()
    assert "[[parent-001]]" in content


def test_forwarded_from_renders_as_wiki_link(sync, tmp_tickets):
    """forwarded_from is rendered as [[original-id]] in frontmatter."""
    ticket = make_ticket(envelope_kwargs={"id": "fwd-002", "forwarded_from": "orig-001"})
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "fwd-002.md").read_text()
    assert "[[orig-001]]" in content


def test_parent_id_none_when_absent(sync, tmp_tickets):
    """parent_id: null in frontmatter when not set."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    fm = _parse_frontmatter(content)
    assert fm["parent_id"] is None


def test_forwarded_from_none_when_absent(sync, tmp_tickets):
    """forwarded_from: null in frontmatter when not set."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    fm = _parse_frontmatter(content)
    assert fm["forwarded_from"] is None


def test_failure_note_ticket_id_is_wiki_link(sync, tmp_tickets):
    """failure_notes entries render their ticket_id as [[wiki-link]]."""
    from datetime import datetime, timezone
    note = FailureNote(
        ticket_id="failed-ticket-001",
        agent="agastya",
        failed_at=datetime.now(timezone.utc).isoformat(),
        reason="Gemma returned multiple functions",
        raw_excerpt="def foo(): ...\ndef bar(): ...",
    )
    env_dict = make_envelope().model_dump()
    env_dict["failure_notes"] = [note.model_dump()]
    ticket = Ticket(envelope=Envelope(**env_dict), payload=make_ticket().payload)
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "[[failed-ticket-001]]" in content


# ---------------------------------------------------------------------------
# Touch history section
# ---------------------------------------------------------------------------

def test_touch_history_empty_shows_placeholder(sync, tmp_tickets):
    """Empty touch_history renders '*(none yet)*' placeholder."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "*(none yet)*" in content


def test_touch_history_populated(sync, tmp_tickets):
    """Touch entries render with agent name, times, and summary."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    entry = TouchEntry(agent="vasishtha", started=now, ended=now, summary="wrote function")
    env_dict = make_envelope().model_dump()
    env_dict["touch_history"] = [entry.model_dump()]
    ticket = Ticket(envelope=Envelope(**env_dict), payload=make_ticket().payload)
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "vasishtha" in content
    assert "wrote function" in content


# ---------------------------------------------------------------------------
# Result section
# ---------------------------------------------------------------------------

def test_result_pending_shows_placeholder(sync, tmp_tickets):
    """No result → '*(pending)*' placeholder."""
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "*(pending)*" in content


def test_result_populated(sync, tmp_tickets):
    """Result fields render in the Result section."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    result = Result(status="success", data="def add_two(a, b): return a + b", produced_at=now, by_agent="vasishtha")
    ticket = make_ticket(result=result)
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "success" in content
    assert "vasishtha" in content


def test_result_data_truncated_at_2000_chars(sync, tmp_tickets):
    """Result.data longer than 2000 chars is truncated in the markdown file."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    long_data = "x" * 3000
    result = Result(status="success", data=long_data, produced_at=now, by_agent="vasishtha")
    ticket = make_ticket(result=result)
    sync.sync_ticket(ticket)
    content = (tmp_tickets / "test-ticket-0001.md").read_text()
    assert "truncated" in content


# ---------------------------------------------------------------------------
# delete_ticket
# ---------------------------------------------------------------------------

def test_delete_ticket_removes_file(sync, tmp_tickets):
    ticket = make_ticket()
    sync.sync_ticket(ticket)
    assert (tmp_tickets / "test-ticket-0001.md").exists()
    sync.delete_ticket("test-ticket-0001")
    assert not (tmp_tickets / "test-ticket-0001.md").exists()


def test_delete_ticket_no_op_if_missing(sync, tmp_tickets):
    """delete_ticket() is silent if the file doesn't exist."""
    sync.delete_ticket("nonexistent-id")  # must not raise


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------

def test_sync_all_writes_one_file_per_ticket(tmp_tickets):
    """sync_all() writes a file for every ticket in the store."""
    store = TicketStore(":memory:")
    sync = MarkdownSync(tickets_dir=tmp_tickets)

    t1 = make_ticket(envelope_kwargs={"id": "bulk-001"})
    t2 = make_ticket(envelope_kwargs={"id": "bulk-002"})
    store.create(t1)
    store.create(t2)

    sync.sync_all(store)

    assert (tmp_tickets / "bulk-001.md").exists()
    assert (tmp_tickets / "bulk-002.md").exists()


def test_sync_all_uses_read_not_list(tmp_tickets):
    """
    sync_all() calls store.read() per ticket, not store.list(), to ensure
    touch_history is populated. Verified by mocking the store.
    """
    mock_store = MagicMock()
    ticket_stub = make_ticket(envelope_kwargs={"id": "mock-001"})
    mock_store.list.return_value = [ticket_stub]
    mock_store.read.return_value = ticket_stub

    sync = MarkdownSync(tickets_dir=tmp_tickets)
    sync.sync_all(mock_store)

    mock_store.read.assert_called_once_with("mock-001")


# ---------------------------------------------------------------------------
# _on_ticket_write wiring
# ---------------------------------------------------------------------------

def test_on_ticket_write_wiring(tmp_tickets):
    """
    Wiring store._on_ticket_write = sync.sync_ticket causes store.create()
    to automatically produce a markdown file.
    """
    store = TicketStore(":memory:")
    sync = MarkdownSync(tickets_dir=tmp_tickets)
    store._on_ticket_write = sync.sync_ticket

    ticket = make_ticket(envelope_kwargs={"id": "wired-001"})
    store.create(ticket)

    assert (tmp_tickets / "wired-001.md").exists()


def test_on_ticket_write_fires_on_update(tmp_tickets):
    """
    store.update() also fires _on_ticket_write, so the markdown file
    stays in sync after status changes.
    """
    store = TicketStore(":memory:")
    sync = MarkdownSync(tickets_dir=tmp_tickets)
    store._on_ticket_write = sync.sync_ticket

    ticket = make_ticket(envelope_kwargs={"id": "wired-002"})
    store.create(ticket)

    # Update status to done
    full = store.read("wired-002")
    env_dict = full.envelope.model_dump()
    env_dict["status"] = "done"
    updated = Ticket(envelope=Envelope(**env_dict), payload=full.payload, result=full.result)
    store.update(updated)

    content = (tmp_tickets / "wired-002.md").read_text()
    assert "status: done" in content
