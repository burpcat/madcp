"""
tests/test_dashboard.py — A4-authored tests for Stage 15.
Run: pytest tests/test_dashboard.py -v
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from madhu.observability.dashboard_data import (
    AgentRow,
    DashboardSnapshot,
    TicketRow,
    TierRow,
)

import dashboard as dash_mod
from dashboard import Dashboard, build_layout, render_snapshot


def _render(renderable) -> str:
    """Render a rich renderable to plain text for assertion."""
    console = Console(force_terminal=True, width=120, color_system=None)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


# ---------------------------------------------------------------------------
# build_layout
# ---------------------------------------------------------------------------

def test_build_layout_regions():
    layout = build_layout()
    required = {"header", "tiers", "agents", "recent", "footer"}
    names = {child.name for child in layout.children}
    assert required == names


# ---------------------------------------------------------------------------
# render_snapshot — basic correctness (no terminal, no Live)
# ---------------------------------------------------------------------------

def _snapshot_with_tickets(*statuses: str) -> DashboardSnapshot:
    rows = [
        TicketRow(
            ticket_id=f"t-{i:03d}",
            tier="Hamsa",
            status=s,
            agent=f"AdHa-agent{i}",
            elapsed_seconds=float(i * 10),
            forwarded_to=None,
        )
        for i, s in enumerate(statuses)
    ]
    return DashboardSnapshot(recent_tickets=rows)


def test_render_waiting_state():
    layout = build_layout()
    snap = DashboardSnapshot(waiting=True)
    render_snapshot(layout, snap, mode="normal", filter_str="")
    tiers_str = _render(layout["tiers"].renderable)
    assert "Waiting" in tiers_str or "waiting" in tiers_str.lower()


def test_render_aborted_distinct_from_killed():
    layout = build_layout()
    snap = _snapshot_with_tickets("aborted", "killed")
    render_snapshot(layout, snap, mode="normal", filter_str="")

    recent_str = _render(layout["recent"].renderable)
    assert "aborted" in recent_str
    assert "killed" in recent_str
    # Style constants must be distinct
    from dashboard import _STATUS_STYLE
    assert _STATUS_STYLE["aborted"] != _STATUS_STYLE["killed"]
    assert _STATUS_STYLE["aborted"] == "dark_orange"
    assert _STATUS_STYLE["killed"] == "red"


def test_render_tail_mode():
    layout = build_layout()
    snap = DashboardSnapshot(log_tail=['{"event": "worker_spawn"}', '{"event": "touch_acquire"}'])
    render_snapshot(layout, snap, mode="tail", filter_str="")
    recent_str = _render(layout["recent"].renderable)
    assert "worker_spawn" in recent_str
    assert "touch_acquire" in recent_str


def test_render_tail_mode_empty():
    layout = build_layout()
    snap = DashboardSnapshot(log_tail=[])
    render_snapshot(layout, snap, mode="tail", filter_str="")
    # Must not crash; content may be empty placeholder
    recent_str = _render(layout["recent"].renderable)
    assert isinstance(recent_str, str)


def test_render_filter_mode_tiers():
    layout = build_layout()
    snap = DashboardSnapshot(
        tiers=[
            TierRow(name="Adi Purusha", active=1, queued=0, in_progress=0),
            TierRow(name="Hamsa", active=2, queued=3, in_progress=2),
        ]
    )
    render_snapshot(layout, snap, mode="filter", filter_str="Hamsa")
    tiers_str = _render(layout["tiers"].renderable)
    assert "Hamsa" in tiers_str
    assert "Adi Purusha" not in tiers_str


def test_render_filter_empty_shows_all():
    layout = build_layout()
    snap = DashboardSnapshot(
        tiers=[
            TierRow(name="Adi Purusha", active=1, queued=0, in_progress=0),
            TierRow(name="Hamsa", active=0, queued=1, in_progress=0),
        ]
    )
    render_snapshot(layout, snap, mode="normal", filter_str="")
    tiers_str = _render(layout["tiers"].renderable)
    assert "Adi Purusha" in tiers_str
    assert "Hamsa" in tiers_str


def test_render_forwarded_shows_arrow():
    layout = build_layout()
    snap = DashboardSnapshot(
        recent_tickets=[
            TicketRow(
                ticket_id="t-orig",
                tier="Hamsa",
                status="forwarded",
                agent="AdHa-vasishtha",
                elapsed_seconds=None,
                forwarded_to="t-succ",
            )
        ]
    )
    render_snapshot(layout, snap, mode="normal", filter_str="")
    recent_str = _render(layout["recent"].renderable)
    assert "t-succ" in recent_str or "→" in recent_str


# ---------------------------------------------------------------------------
# Dashboard key handling (no terminal, no Live, no SQLite)
# ---------------------------------------------------------------------------

def _make_dashboard() -> Dashboard:
    with patch("dashboard.Live"):
        d = Dashboard(db_path="/tmp/fake.db", log_path="/tmp/fake.jsonl")
    return d


def test_dashboard_stop_on_q():
    d = _make_dashboard()
    assert not d._stop.is_set()
    d._handle_key("q")
    assert d._stop.is_set()


def test_dashboard_stop_on_ctrl_c():
    d = _make_dashboard()
    d._handle_key("\x03")
    assert d._stop.is_set()


def test_dashboard_tail_mode_toggle():
    d = _make_dashboard()
    assert d.mode == "normal"
    d._handle_key("t")
    assert d.mode == "tail"
    d._handle_key("t")
    assert d.mode == "normal"


def test_dashboard_filter_toggle():
    d = _make_dashboard()
    assert d.mode == "normal"
    d._handle_key("f")
    assert d.mode == "filter"
    d._handle_key("f")
    assert d.mode == "normal"


def test_dashboard_r_sets_force_tick():
    d = _make_dashboard()
    assert not d._force_tick.is_set()
    d._handle_key("r")
    assert d._force_tick.is_set()


def test_dashboard_tick_calls_fetch():
    d = _make_dashboard()
    fake_snap = DashboardSnapshot()
    with patch("dashboard.fetch_snapshot", return_value=fake_snap) as mock_dash_fetch:
        with patch("dashboard.render_snapshot"):
            d._tick()
        mock_dash_fetch.assert_called_once_with(d.db_path, d.log_path)


def test_dashboard_filter_str_sets_filter_mode():
    with patch("dashboard.Live"):
        d = Dashboard(db_path="/tmp/fake.db", filter_str="Hamsa")
    assert d.mode == "filter"
    assert d.filter_str == "Hamsa"


# ---------------------------------------------------------------------------
# Import isolation (subprocess — clean interpreter)
# ---------------------------------------------------------------------------

def test_dashboard_imports_no_store():
    code = (
        "import dashboard, sys; "
        "bad = [m for m in ['madhu.store.sqlite', 'madhu.scheduler', 'madhu.workers'] "
        "       if m in sys.modules]; "
        "assert not bad, f'Unexpected imports: {bad}'; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Import isolation failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout