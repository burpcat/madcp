# tests/test_jsonl.py
"""
Tests for madhu/observability/jsonl.py — Stage 13.

Covered:
    RunLogger.__init__: directory auto-creation, path storage, idempotent re-init
    RunLogger.log: schema correctness, append semantics, null serialization,
                   default=str fallback, no-buffering, write-failure swallowing,
                   thread safety

Deferred to C3 integration checkpoint:
    Cross-process concurrent writes (multiple worker subprocesses)
    Wiring: scheduler spawn/exit, touch acquire/release/forward,
            worker ollama_call/ollama_result, server mcp_submit_enter/mcp_submit_exit
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from madhu.observability.jsonl import RunLogger


# ---------------------------------------------------------------------------
# TestRunLoggerInit
# ---------------------------------------------------------------------------

class TestRunLoggerInit:
    def test_directory_created_if_missing(self, tmp_path):
        path = tmp_path / "sub" / "nested" / "runs.jsonl"
        RunLogger(path)
        assert path.parent.is_dir()

    def test_path_stored_as_path_object(self, tmp_path):
        path = tmp_path / "runs.jsonl"
        logger = RunLogger(path)
        assert logger._path == path

    def test_string_path_coerced_to_path(self, tmp_path):
        path_str = str(tmp_path / "runs.jsonl")
        logger = RunLogger(path_str)
        assert logger._path == Path(path_str)

    def test_existing_directory_does_not_raise(self, tmp_path):
        """mkdir(parents=True, exist_ok=True) must not raise on second construction."""
        path = tmp_path / "runs.jsonl"
        RunLogger(path)
        RunLogger(path)  # must not raise


# ---------------------------------------------------------------------------
# TestRunLoggerLog
# ---------------------------------------------------------------------------

class TestRunLoggerLog:
    def _read_entries(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_log_produces_valid_json_line(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn", ticket_id="t-001")
        lines = (tmp_path / "test.jsonl").read_text().splitlines()
        assert len(lines) == 1
        json.loads(lines[0])  # raises if invalid

    def test_log_all_five_schema_fields_present(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn")
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert {"timestamp", "event_type", "ticket_id", "agent_name", "details"} <= entry.keys()

    def test_log_event_type_recorded(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("mcp_submit_enter", ticket_id="t-002")
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert entry["event_type"] == "mcp_submit_enter"

    def test_log_ticket_id_and_agent_name_recorded(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("touch_acquire", ticket_id="t-abc", agent_name="AdHa-agastya")
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert entry["ticket_id"] == "t-abc"
        assert entry["agent_name"] == "AdHa-agastya"

    def test_log_omitted_optional_fields_serialize_as_null(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn")
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert entry["ticket_id"] is None
        assert entry["agent_name"] is None
        assert entry["details"] is None

    def test_log_details_dict_recorded(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn", details={"pid": 12345, "tier_name": "Hamsa"})
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert entry["details"] == {"pid": 12345, "tier_name": "Hamsa"}

    def test_log_appends_multiple_lines_each_valid_json(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn", ticket_id="t-001")
        logger.log("worker_exit", ticket_id="t-001", details={"exit_code": 0})
        entries = self._read_entries(tmp_path / "test.jsonl")
        assert len(entries) == 2
        assert entries[0]["event_type"] == "worker_spawn"
        assert entries[1]["event_type"] == "worker_exit"

    def test_log_no_buffering_line_visible_immediately_after_call(self, tmp_path):
        """Each log() call must be durable on disk before it returns — no buffering."""
        log_path = tmp_path / "test.jsonl"
        logger = RunLogger(log_path)
        logger.log("worker_spawn", ticket_id="t-001")
        # File must be non-empty immediately — no explicit flush required.
        assert log_path.stat().st_size > 0
        logger.log("worker_exit", ticket_id="t-001")
        assert len(log_path.read_text().splitlines()) == 2

    def test_log_timestamp_is_utc_iso8601(self, tmp_path):
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn")
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.utcoffset() is not None, "timestamp must carry UTC offset"
        assert ts.utcoffset().total_seconds() == 0, "UTC offset must be zero"

    def test_log_nonserializable_detail_uses_str_fallback(self, tmp_path):
        """Path objects in details must not crash the logger; default=str handles them."""
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("worker_spawn", details={"log_path": Path("/var/log/madhu")})
        entry = self._read_entries(tmp_path / "test.jsonl")[0]
        assert entry["details"]["log_path"] == "/var/log/madhu"

    def test_log_never_raises_on_write_failure(self, tmp_path, capsys):
        """log() must swallow OSError and report to stderr — never propagate."""
        logger = RunLogger(tmp_path / "test.jsonl")
        with patch("builtins.open", side_effect=OSError("disk full")):
            logger.log("worker_spawn")  # must not raise
        captured = capsys.readouterr()
        assert "RunLogger" in captured.err
        assert "worker_spawn" in captured.err

    def test_log_thread_safe_all_lines_valid_json(self, tmp_path):
        """Concurrent writes from multiple threads must each produce a complete, valid JSON line."""
        log_path = tmp_path / "test.jsonl"
        logger = RunLogger(log_path)

        def write_batch():
            for i in range(20):
                logger.log(
                    "ollama_call",
                    ticket_id=f"t-{i:03}",
                    details={"n": i, "padding": "x" * 200},
                )

        threads = [threading.Thread(target=write_batch) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = log_path.read_text().splitlines()
        assert len(lines) == 100, f"expected 100 lines, got {len(lines)}"
        for i, line in enumerate(lines):
            json.loads(line)  # raises if any line is truncated or interleaved

    def test_log_two_events_have_ordered_or_equal_timestamps(self, tmp_path):
        """Timestamps must be monotonically non-decreasing across sequential calls."""
        logger = RunLogger(tmp_path / "test.jsonl")
        logger.log("mcp_submit_enter", ticket_id="t-001")
        logger.log("mcp_submit_exit", ticket_id="t-001")
        entries = self._read_entries(tmp_path / "test.jsonl")
        t0 = datetime.fromisoformat(entries[0]["timestamp"])
        t1 = datetime.fromisoformat(entries[1]["timestamp"])
        assert t1 >= t0, f"second timestamp {t1} precedes first {t0}"
