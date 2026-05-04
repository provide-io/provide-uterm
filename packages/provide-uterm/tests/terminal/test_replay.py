#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for replay utilities."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest

from provide.terminal.replay.raw import rebuild_raw_stream

if TYPE_CHECKING:
    from pathlib import Path


def _make_log(tmp_path: Path, records: list[dict]) -> Path:
    log_path = tmp_path / "test.jsonl"
    with log_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return log_path


class TestRebuildRawStream:
    def test_combines_read_events(self, tmp_path: Path) -> None:
        chunk1 = b"Hello "
        chunk2 = b"World"
        records = [
            {"event": "read", "ts": 1.0, "data": {"raw_bytes_b64": base64.b64encode(chunk1).decode()}},
            {"event": "read", "ts": 2.0, "data": {"raw_bytes_b64": base64.b64encode(chunk2).decode()}},
        ]
        log_path = _make_log(tmp_path, records)
        out_path = tmp_path / "out.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b"Hello World"

    def test_skips_non_read_events(self, tmp_path: Path) -> None:
        records = [
            {"event": "send", "ts": 1.0, "data": {"keys": "x", "bytes_b64": base64.b64encode(b"x").decode()}},
            {"event": "read", "ts": 2.0, "data": {"raw_bytes_b64": base64.b64encode(b"response").decode()}},
        ]
        log_path = _make_log(tmp_path, records)
        out_path = tmp_path / "out.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b"response"

    def test_skips_wire_control_events(self, tmp_path: Path) -> None:
        records = [
            {"event": "wire_send", "ts": 1.0, "data": {"bytes_b64": base64.b64encode(b"wire").decode()}},
            {"event": "control_recv", "ts": 1.5, "data": {"control": {"type": "hello"}}},
            {"event": "read", "ts": 2.0, "data": {"raw_bytes_b64": base64.b64encode(b"response").decode()}},
        ]
        log_path = _make_log(tmp_path, records)
        out_path = tmp_path / "out-wire.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b"response"

    def test_empty_log_produces_empty_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "empty.jsonl"
        log_path.write_text("")
        out_path = tmp_path / "out.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b""

    def test_read_event_without_data_key_is_skipped(self, tmp_path: Path) -> None:
        """Kill mutmut_26/28: get("data", None) crashes when "data" key is missing."""
        records = [
            {"event": "read", "ts": 1.0},  # no "data" key at all
            {"event": "read", "ts": 2.0, "data": {"raw_bytes_b64": base64.b64encode(b"hi").decode()}},
        ]
        log_path = _make_log(tmp_path, records)
        out_path = tmp_path / "out.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b"hi"

    def test_read_event_without_raw_bytes_b64_produces_no_output(self, tmp_path: Path) -> None:
        """Kill mutmut_33: default "XXXX" instead of "" causes garbage bytes to be written."""
        records = [
            {"event": "read", "ts": 1.0, "data": {}},  # data present but no raw_bytes_b64
        ]
        log_path = _make_log(tmp_path, records)
        out_path = tmp_path / "out.bin"
        rebuild_raw_stream(log_path, out_path)
        assert out_path.read_bytes() == b""


class TestRebuildRawStreamBlankLines:
    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        import base64
        import json

        log = tmp_path / "session.jsonl"
        log.write_text(
            json.dumps({"event": "read", "data": {"raw_bytes_b64": base64.b64encode(b"hi").decode()}})
            + "\n\n"  # blank line in middle
            + json.dumps({"event": "read", "data": {"raw_bytes_b64": base64.b64encode(b"!").decode()}})
            + "\n"
        )
        out = tmp_path / "raw.bin"
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == b"hi!"


class TestReplayLog:
    def test_replay_log_renders_screens(self, tmp_path: Path, capsys) -> None:
        import json

        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "session.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "Hello World"}})
            + "\n"
            + json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "Second Frame"}})
            + "\n"
        )
        replay_log(log, speed=100.0)
        captured = capsys.readouterr()
        assert "Hello World" in captured.out or "Second Frame" in captured.out

    def test_replay_log_skips_non_matching_events(self, tmp_path: Path, capsys) -> None:
        import json

        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "session.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "send", "data": {"screen": "Should Not Appear"}}) + "\n")
        replay_log(log, speed=100.0)
        captured = capsys.readouterr()
        assert "Should Not Appear" not in captured.out

    def test_replay_log_step_mode(self, tmp_path: Path, monkeypatch) -> None:
        import json

        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "session.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "Frame 1"}}) + "\n")
        monkeypatch.setattr("builtins.input", lambda _: "")
        # Should not raise
        replay_log(log, step=True)

    def test_replay_log_skips_records_without_screen(self, tmp_path: Path, capsys) -> None:
        import json

        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "session.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "screen", "data": {}}) + "\n")
        replay_log(log, speed=100.0)
        # Should not raise, no screen key → skipped

    def test_replay_log_skips_blank_lines(self, tmp_path: Path, capsys) -> None:
        """Blank lines in the log file are skipped (covers line 45)."""
        import json

        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "session.jsonl"
        log.write_text(
            "\n"  # blank line — triggers line 45
            + json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "Frame1"}})
            + "\n"
        )
        replay_log(log, speed=100.0)
        captured = capsys.readouterr()
        assert "Frame1" in captured.out

    def test_replay_log_sleeps_between_frames(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """time.sleep is called when delta > 0 between frames (covers line 56)."""
        import json

        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        log = tmp_path / "session.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "Frame1"}})
            + "\n"
            + json.dumps({"ts": 3.0, "event": "screen", "data": {"screen": "Frame2"}})
            + "\n"
        )
        replay_log(log, speed=1.0)
        # Should have slept once for the 2s gap between frames
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(2.0)
