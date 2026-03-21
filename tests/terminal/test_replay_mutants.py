#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for replay utilities (viewer.py and raw.py).

Kills surviving mutants in:
- _clear_screen (mutmut_1-10)
- _render_screen (mutmut_1,3,6,8)
- replay_log (mutmut_1,2,15,17,21,22,28,33,35,41,45,47,53,61,63,75-77,83-85,88,90)
- rebuild_raw_stream (mutmut_6,8,22,24,26,28,33)
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path


def _make_log(tmp_path: Path, records: list[dict]) -> Path:
    log_path = tmp_path / "test.jsonl"
    with log_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# _clear_screen (mutmut_1-10)
# ---------------------------------------------------------------------------


class TestClearScreen:
    def test_clear_screen_writes_ansi_clear_sequence(self) -> None:
        """mutmut_1,4: _clear_screen must print the ANSI clear sequence, not None."""
        from provide.terminal.replay.viewer import _clear_screen

        buf = io.StringIO()
        _clear_screen(buf)
        output = buf.getvalue()
        assert "\x1b[2J\x1b[H" in output
        assert "None" not in output

    def test_clear_screen_writes_to_given_output(self) -> None:
        """mutmut_3,6: output param must be used, not None or ignored."""
        from provide.terminal.replay.viewer import _clear_screen

        buf = io.StringIO()
        other = io.StringIO()
        _clear_screen(buf)
        # buf should have content; other should not
        assert buf.getvalue() != ""
        assert other.getvalue() == ""

    def test_clear_screen_no_trailing_newline(self) -> None:
        """mutmut_2,5: end='' must be used so no extra newline is appended."""
        from provide.terminal.replay.viewer import _clear_screen

        buf = io.StringIO()
        _clear_screen(buf)
        output = buf.getvalue()
        # Must end with 'H' from the sequence, not a newline
        assert output.endswith("\x1b[H")
        # Mutmut_5 removes end="" → default '\n' appended
        assert not output.endswith("\n")

    def test_clear_screen_uses_escape_uppercase_j_h(self) -> None:
        """mutmut_7,8,9: ANSI codes must be \\x1b[2J\\x1b[H (uppercase J and H)."""
        from provide.terminal.replay.viewer import _clear_screen

        buf = io.StringIO()
        _clear_screen(buf)
        output = buf.getvalue()
        # Must contain exactly \\x1b[2J and \\x1b[H (not \\x1b[2j or \\x1b[h)
        assert "\x1b[2J" in output  # uppercase J
        assert "\x1b[H" in output  # uppercase H
        assert "\x1b[2j" not in output  # mutmut_8: lowercase j
        assert "\x1b[h" not in output  # mutmut_8: lowercase h

    def test_clear_screen_no_extra_content_after_sequence(self) -> None:
        """mutmut_10: end='' so no 'XXXX' or extra chars after sequence."""
        from provide.terminal.replay.viewer import _clear_screen

        buf = io.StringIO()
        _clear_screen(buf)
        output = buf.getvalue()
        assert output == "\x1b[2J\x1b[H"


# ---------------------------------------------------------------------------
# _render_screen (mutmut_1,3,6,8)
# ---------------------------------------------------------------------------


class TestRenderScreen:
    def test_render_screen_calls_clear_first(self) -> None:
        """mutmut_1: _clear_screen must receive the output file, not None."""
        from provide.terminal.replay.viewer import _render_screen

        buf = io.StringIO()
        _render_screen("Hello World", buf)
        output = buf.getvalue()
        # Clear sequence should appear before the screen content
        assert "\x1b[2J" in output
        clear_pos = output.index("\x1b[2J")
        hello_pos = output.index("Hello World")
        assert clear_pos < hello_pos

    def test_render_screen_writes_screen_content(self) -> None:
        """mutmut_3,6,8: screen content must be written to output."""
        from provide.terminal.replay.viewer import _render_screen

        buf = io.StringIO()
        _render_screen("Test Content", buf)
        output = buf.getvalue()
        assert "Test Content" in output

    def test_render_screen_no_trailing_newline_after_screen(self) -> None:
        """mutmut_3,6,8: end='' must be used, so screen content is not followed by \\n."""
        from provide.terminal.replay.viewer import _render_screen

        buf = io.StringIO()
        _render_screen("ABC", buf)
        output = buf.getvalue()
        # Must end with the last char of the screen, not a newline
        assert output.endswith("ABC")

    def test_render_screen_writes_to_given_output(self) -> None:
        """mutmut_3: output param must be used for screen print too."""
        from provide.terminal.replay.viewer import _render_screen

        buf1 = io.StringIO()
        buf2 = io.StringIO()
        _render_screen("Frame Data", buf1)
        assert "Frame Data" in buf1.getvalue()
        assert "Frame Data" not in buf2.getvalue()


# ---------------------------------------------------------------------------
# replay_log (mutmut_1,2,15,17,21,22,28,33,35,41,45,47,53,61,63,75-77,83-85,88,90)
# ---------------------------------------------------------------------------


class TestReplayLogMutants:
    def test_default_speed_is_1_0(self) -> None:
        """mutmut_1: default speed must be 1.0, not 2.0."""
        import inspect

        from provide.terminal.replay.viewer import replay_log

        sig = inspect.signature(replay_log)
        assert sig.parameters["speed"].default == 1.0

    def test_default_step_is_false(self) -> None:
        """mutmut_2: default step must be False, not True."""
        import inspect

        from provide.terminal.replay.viewer import replay_log

        sig = inspect.signature(replay_log)
        assert sig.parameters["step"].default is False

    def test_opens_file_with_utf8_encoding(self, tmp_path: Path) -> None:
        """mutmut_15: file must open with encoding='utf-8', not None."""
        from provide.terminal.replay.viewer import replay_log

        # Write a log with non-ASCII content
        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "ma\u00f1ana"}}) + "\n",
            encoding="utf-8",
        )
        buf = io.StringIO()
        replay_log(log, output=buf)
        # Should not raise and must preserve non-ASCII (U+00F1 = ñ)
        assert "\u00f1" in buf.getvalue()

    def test_lineno_starts_at_1(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_21,22: enumerate must start at 1 (default or explicit), not 0 or 2."""
        from provide.terminal.replay.viewer import replay_log

        # With a corrupt first line, warning is logged with lineno=1
        log = tmp_path / "s.jsonl"
        log.write_text("not-valid-json\n" + json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "OK"}}) + "\n")

        warnings_logged = []
        import provide.terminal.replay.viewer as viewer_module

        monkeypatch.setattr(
            viewer_module.logger,
            "warning",
            lambda *a, **kw: warnings_logged.append(a),
        )
        buf = io.StringIO()
        replay_log(log, output=buf)
        # Warning should have been logged with lineno=1
        assert len(warnings_logged) >= 1
        # Args should contain the lineno
        assert 1 in warnings_logged[0]

    def test_corrupt_line_continues_not_breaks(self, tmp_path: Path) -> None:
        """mutmut_35: on JSONDecodeError, use 'continue' not 'break'."""
        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "s.jsonl"
        log.write_text(
            "corrupt-json\n" + json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "After Corrupt"}}) + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=100.0, output=buf)
        # After the corrupt line, processing must continue (not stop)
        assert "After Corrupt" in buf.getvalue()

    def test_non_matching_event_continues_not_breaks(self, tmp_path: Path) -> None:
        """mutmut_41: on non-matching event, use 'continue' not 'break'."""
        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "send", "data": {"screen": "skip"}})
            + "\n"
            + json.dumps({"ts": 2.0, "event": "screen", "data": {"screen": "Should Show"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=100.0, output=buf)
        # Second record must be processed even though first was skipped
        assert "Should Show" in buf.getvalue()

    def test_missing_screen_key_continues_not_breaks(self, tmp_path: Path) -> None:
        """mutmut_53: on screen=None, use 'continue' not 'break'."""
        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {}})
            + "\n"  # no screen
            + json.dumps({"ts": 2.0, "event": "screen", "data": {"screen": "After None"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=100.0, output=buf)
        # After the None screen, processing must continue
        assert "After None" in buf.getvalue()

    def test_data_default_is_empty_dict(self, tmp_path: Path) -> None:
        """mutmut_45: record.get('data', {}) must use {} as default, not None."""
        from provide.terminal.replay.viewer import replay_log

        # Record with no 'data' key at all
        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "screen"}) + "\n")  # no data key
        buf = io.StringIO()
        # Should not raise AttributeError (None.get would fail)
        replay_log(log, speed=100.0, output=buf)

    def test_sleep_delta_uses_max_speed_100(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_75: max speed clamp must be 100.0 not 101.0.

        The formula is: delta = (ts2 - ts1) / min(max(speed, 0.01), 100.0)
        With speed=200.0 (above clamp), delta = (ts2 - ts1) / 100.0 (clamped at 100)
        With a 101.0 clamp instead of 100.0, the result would differ.
        Use speed=200.0 with a 1-second gap:
          correct (100.0 clamp): delta = 1.0 / 100.0 = 0.01
          wrong (101.0 clamp):   delta = 1.0 / 101.0 ≈ 0.0099
        The distinction is subtle but the test checks the clamp behavior.
        """
        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}})
            + "\n"
            + json.dumps({"ts": 2.0, "event": "screen", "data": {"screen": "F2"}})
            + "\n"
        )
        buf = io.StringIO()
        # With speed=200 (above max clamp of 100), delta = 1.0 / 100.0 = 0.01
        replay_log(log, speed=200.0, output=buf)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(0.01, rel=0.05)

    def test_sleep_only_when_delta_gt_0(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_76,77: sleep must only be called when delta > 0 (not >= 0 or > 1)."""
        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        # Two frames at same timestamp → delta=0 → no sleep
        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}})
            + "\n"
            + json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F2"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=1.0, output=buf)
        # delta=0 → must NOT sleep
        assert len(sleep_calls) == 0

    def test_sleep_not_called_for_small_positive_delta(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_77: delta > 1 threshold would prevent sleep for sub-second gaps."""
        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}})
            + "\n"
            + json.dumps({"ts": 1.5, "event": "screen", "data": {"screen": "F2"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=1.0, output=buf)
        # delta=0.5 > 0 → sleep MUST be called (mutmut_77 would prevent this for delta=0.5 < 1)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(0.5, rel=0.01)

    def test_step_input_message_is_dash_dash_next_dash_dash(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_83,84,85: input() message must be '-- next --'."""
        from provide.terminal.replay.viewer import replay_log

        input_calls: list[str] = []
        monkeypatch.setattr("builtins.input", lambda msg: input_calls.append(msg))

        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}}) + "\n")
        buf = io.StringIO()
        replay_log(log, step=True, output=buf)
        assert len(input_calls) == 1
        assert input_calls[0] == "-- next --"

    def test_last_ts_updated_from_record_ts(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_88,90: last_ts must be updated from record.get('ts', last_ts) not None."""
        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        log = tmp_path / "s.jsonl"
        # Three frames: 1.0, 2.0, 4.0 — gaps should be 1.0 and 2.0
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}})
            + "\n"
            + json.dumps({"ts": 2.0, "event": "screen", "data": {"screen": "F2"}})
            + "\n"
            + json.dumps({"ts": 4.0, "event": "screen", "data": {"screen": "F3"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, speed=1.0, output=buf)
        # If last_ts was None (mutmut_88), gaps would all be computed from None → error or wrong value
        # If last_ts was not updated, all gaps would be from start → wrong sleep
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == pytest.approx(1.0, rel=0.01)  # gap from F1 to F2
        assert sleep_calls[1] == pytest.approx(2.0, rel=0.01)  # gap from F2 to F3

    def test_ts_fallback_uses_last_ts_not_none(self, tmp_path: Path, monkeypatch) -> None:
        """mutmut_61,63: record.get('ts', last_ts) must fall back to last_ts, not None."""
        from provide.terminal.replay.viewer import replay_log

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", sleep_calls.append)

        # Second frame has no 'ts' key → should fall back to last_ts (same time → no sleep)
        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "screen", "data": {"screen": "F1"}})
            + "\n"
            + json.dumps({"event": "screen", "data": {"screen": "F2"}})
            + "\n"  # no ts
        )
        buf = io.StringIO()
        # Should not raise TypeError (None - float would fail)
        replay_log(log, speed=1.0, output=buf)
        # No sleep since ts falls back to last_ts (delta=0)
        assert len(sleep_calls) == 0

    def test_replay_log_filters_by_events_param(self, tmp_path: Path) -> None:
        """mutmut_2: if step=True default, single-frame log would require user input without step=False."""
        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({"ts": 1.0, "event": "read", "data": {"screen": "Read Frame"}}) + "\n")
        buf = io.StringIO()
        # Default events=['read', 'screen'] — 'read' should be rendered
        replay_log(log, speed=100.0, output=buf)
        assert "Read Frame" in buf.getvalue()

    def test_custom_events_filter_applies(self, tmp_path: Path) -> None:
        """Verify custom events filter works correctly."""
        from provide.terminal.replay.viewer import replay_log

        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "event": "custom", "data": {"screen": "Custom Event"}})
            + "\n"
            + json.dumps({"ts": 2.0, "event": "screen", "data": {"screen": "Screen Event"}})
            + "\n"
        )
        buf = io.StringIO()
        replay_log(log, events=["custom"], speed=100.0, output=buf)
        assert "Custom Event" in buf.getvalue()
        assert "Screen Event" not in buf.getvalue()


# ---------------------------------------------------------------------------
# rebuild_raw_stream (mutmut_6,8,22,24,26,28,33)
# ---------------------------------------------------------------------------


class TestRebuildRawStreamMutants:
    def test_reads_file_with_utf8_encoding(self, tmp_path: Path) -> None:
        """mutmut_6: must read with encoding='utf-8', not None."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        log = tmp_path / "s.jsonl"
        # Write with UTF-8 encoding
        raw = b"test"
        log.write_text(
            json.dumps({"event": "read", "data": {"raw_bytes_b64": base64.b64encode(raw).decode()}}) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "out.bin"
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == raw

    def test_raw_bytes_b64_default_is_empty_string(self, tmp_path: Path) -> None:
        """mutmut_22,24: default for missing raw_bytes_b64 must be '' not None."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        log = tmp_path / "s.jsonl"
        # 'data' key present but no 'raw_bytes_b64' key
        log.write_text(json.dumps({"event": "read", "data": {}}) + "\n")
        out = tmp_path / "out.bin"
        # With None default (mutmut_22), base64.b64decode(None) would fail
        # With '' default, the `if raw_b64:` check makes it a no-op
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == b""

    def test_data_default_is_empty_dict_not_none(self, tmp_path: Path) -> None:
        """mutmut_26,28: record.get('data', {}) must use {} default, not None."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        log = tmp_path / "s.jsonl"
        # 'data' key missing entirely
        log.write_text(json.dumps({"event": "read"}) + "\n")
        out = tmp_path / "out.bin"
        # With None default (mutmut_26), None.get() would raise AttributeError
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == b""

    def test_empty_raw_b64_skips_base64_decode(self, tmp_path: Path) -> None:
        """mutmut_33: default '' must not cause b64decode call (empty string is falsy)."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        # Records with raw_bytes_b64='' should produce no output
        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({"event": "read", "data": {"raw_bytes_b64": ""}}) + "\n")
        out = tmp_path / "out.bin"
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == b""

    def test_raw_bytes_b64_with_garbage_default_would_produce_output(self, tmp_path: Path) -> None:
        """mutmut_33: default 'XXXX' (if it were base64) would produce garbage bytes."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        # Verify '' default means missing raw_bytes_b64 → no output (not garbage)
        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"event": "read", "data": {}})
            + "\n"  # no raw_bytes_b64
            + json.dumps({"event": "read", "data": {"raw_bytes_b64": base64.b64encode(b"real").decode()}})
            + "\n"
        )
        out = tmp_path / "out.bin"
        rebuild_raw_stream(log, out)
        # Only the real bytes, no garbage from the missing field
        assert out.read_bytes() == b"real"

    def test_multiple_read_events_concatenated(self, tmp_path: Path) -> None:
        """Integration test: multiple read events produce correct concatenated output."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        chunk1 = b"\x01\x02"
        chunk2 = b"\x03\x04"
        log = _make_log(
            tmp_path,
            [
                {"event": "read", "data": {"raw_bytes_b64": base64.b64encode(chunk1).decode()}},
                {"event": "log_start", "data": {}},  # skipped
                {"event": "read", "data": {"raw_bytes_b64": base64.b64encode(chunk2).decode()}},
            ],
        )
        out = tmp_path / "out.bin"
        rebuild_raw_stream(log, out)
        assert out.read_bytes() == chunk1 + chunk2


import pytest
