# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLA. All Rights Reserved. See LICENSE.
from __future__ import annotations

import argparse
import signal
import sys
import termios
import threading
import tty
from datetime import timedelta

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from madhu.observability.dashboard_data import (
    DashboardSnapshot,
    fetch_snapshot,
)

_DEFAULT_DB = "data/palakudu.db"
_DEFAULT_LOG = "logs/runs.jsonl"


# ---------------------------------------------------------------------------
# Layout construction — called once at startup; mutated on every tick
# ---------------------------------------------------------------------------

def build_layout() -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="tiers", size=8),
        Layout(name="agents", size=8),
        Layout(name="recent", minimum_size=6),
        Layout(name="footer", size=3),
    )
    return layout


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _elapsed_str(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    h, rem = divmod(int(max(0.0, seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_STATUS_STYLE: dict[str, str] = {
    "done":        "green",
    "in_progress": "cyan",
    "queued":      "white",
    "touched":     "yellow",
    "failed":      "red",
    "killed":      "red",
    "aborted":     "dark_orange",
    "forwarded":   "yellow",
}

_STATUS_ICON: dict[str, str] = {
    "done":        "✔",
    "failed":      "✗",
    "aborted":     "✗",
    "killed":      "✗",
    "forwarded":   "⚠",
    "in_progress": "▶",
    "queued":      "○",
    "touched":     "◉",
}


def render_snapshot(
    layout: Layout,
    snapshot: DashboardSnapshot,
    mode: str,
    filter_str: str,
) -> None:
    layout["header"].update(
        Panel(Text("MadCP — madhu", style="bold white"), style="blue")
    )

    if mode == "tail":
        footer_text = "[t] exit tail  [q] quit  [r] refresh"
    elif mode == "filter":
        footer_text = (
            f"[filter: {filter_str or '(all)'}]  "
            "[f] clear filter  [q] quit  [r] refresh"
        )
    else:
        footer_text = "[q] quit  [r] refresh  [f] filter  [t] tail logs"
    layout["footer"].update(Panel(Text(footer_text, style="dim")))

    if snapshot.waiting:
        layout["tiers"].update(
            Panel(Text("Waiting for database…", style="dim italic"), title="TIERS")
        )
        layout["agents"].update(
            Panel(Text("", style="dim"), title="LIVE AGENTS")
        )
        layout["recent"].update(
            Panel(Text("", style="dim"), title="RECENT TICKETS (last 5)")
        )
        return

    # --- TIERS ---
    tier_table = Table(
        show_header=True, header_style="bold", box=None, padding=(0, 1)
    )
    tier_table.add_column("Tier",        style="white", min_width=14)
    tier_table.add_column("Active",      justify="center")
    tier_table.add_column("Queued",      justify="center")
    tier_table.add_column("In Progress", justify="center")
    for t in snapshot.tiers:
        if filter_str and filter_str not in t.name:
            continue
        tier_table.add_row(
            t.name, str(t.active), str(t.queued), str(t.in_progress)
        )
    layout["tiers"].update(Panel(tier_table, title="TIERS"))

    # --- LIVE AGENTS ---
    agent_table = Table(
        show_header=True, header_style="bold", box=None, padding=(0, 1)
    )
    agent_table.add_column("Agent",   style="cyan", min_width=18)
    agent_table.add_column("Tier",    min_width=10)
    agent_table.add_column("Ticket",  min_width=8)
    agent_table.add_column("Elapsed", justify="right")
    for a in snapshot.agents:
        if filter_str and filter_str not in a.tier and filter_str not in a.agent_name:
            continue
        agent_table.add_row(
            a.agent_name,
            a.tier,
            a.ticket_id,
            _elapsed_str(a.elapsed_seconds),
        )
    layout["agents"].update(Panel(agent_table, title="LIVE AGENTS"))

    # --- RECENT TICKETS / LOG TAIL ---
    if mode == "tail":
        body = (
            "\n".join(snapshot.log_tail)
            if snapshot.log_tail
            else "(no log entries yet)"
        )
        layout["recent"].update(
            Panel(Text(body, style="dim"), title="LOG TAIL (last 20)")
        )
    else:
        recent_table = Table(show_header=False, box=None, padding=(0, 1))
        for t in snapshot.recent_tickets:
            if filter_str and filter_str not in t.tier and filter_str not in t.status:
                continue
            icon  = _STATUS_ICON.get(t.status, "?")
            style = _STATUS_STYLE.get(t.status, "white")
            if t.forwarded_to:
                extra = f"(→ {t.forwarded_to})"
            elif t.status == "aborted":
                extra = "(aborted — fwd limit)"
            else:
                extra = _elapsed_str(t.elapsed_seconds)
            recent_table.add_row(
                Text(f"{icon} {t.ticket_id}", style=style),
                Text(t.tier),
                Text(t.status, style=style),
                Text(t.agent, style="cyan"),
                Text(extra, style="dim"),
            )
        layout["recent"].update(
            Panel(recent_table, title="RECENT TICKETS (last 5)")
        )


# ---------------------------------------------------------------------------
# Dashboard class
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(
        self,
        db_path: str,
        log_path: str = _DEFAULT_LOG,
        filter_str: str = "",
    ) -> None:
        self.db_path = db_path
        self.log_path = log_path
        self.filter_str = filter_str
        self.mode: str = "filter" if filter_str else "normal"
        self.layout = build_layout()
        self.live = Live(self.layout, refresh_per_second=1, screen=True)
        self._stop = threading.Event()
        self._force_tick = threading.Event()

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            # Set raw mode in the main thread — before the key thread starts —
            # so the finally block restores atomically with respect to setraw.
            tty.setraw(fd)
            key_thread = threading.Thread(target=self._read_keys, daemon=True)
            with self.live:
                key_thread.start()
                while not self._stop.is_set():
                    self._tick()
                    self._force_tick.wait(timeout=1.0)
                    self._force_tick.clear()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _tick(self) -> None:
        snapshot = fetch_snapshot(self.db_path, self.log_path)
        render_snapshot(self.layout, snapshot, self.mode, self.filter_str)

    def _read_keys(self) -> None:
        # Terminal is already in raw mode (set by main thread).
        try:
            while not self._stop.is_set():
                ch = sys.stdin.read(1)
                if ch:
                    self._handle_key(ch)
        except Exception:
            pass

    def _handle_key(self, ch: str) -> None:
        if ch in ("q", "Q", "\x03"):       # q or Ctrl-C
            self._stop.set()
        elif ch in ("r", "R"):
            self._force_tick.set()
        elif ch in ("t", "T"):
            self.mode = "normal" if self.mode == "tail" else "tail"
            self._force_tick.set()
        elif ch in ("f", "F"):
            self.mode = "normal" if self.mode == "filter" else "filter"
            self._force_tick.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="MadCP terminal dashboard")
    parser.add_argument("--db",     default=_DEFAULT_DB,  help="Path to SQLite db")
    parser.add_argument("--log",    default=_DEFAULT_LOG,  help="Path to JSONL log")
    parser.add_argument(
        "--filter",
        dest="filter_str",
        default="",
        help=(
            "Pre-set filter string (tier name or status). "
            "'f' toggles it on/off at runtime."
        ),
    )
    args = parser.parse_args()

    dash = Dashboard(
        db_path=args.db,
        log_path=args.log,
        filter_str=args.filter_str,
    )

    def _sigterm(sig, frame):  # noqa: ANN001
        dash._stop.set()

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        dash.run()
    except KeyboardInterrupt:
        dash._stop.set()


if __name__ == "__main__":
    main()