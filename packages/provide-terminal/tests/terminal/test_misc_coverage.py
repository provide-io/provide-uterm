#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Simple coverage gap tests: protocols, __init__, screen, replay, session_logger,
models, polling, base, ansi, emulator, cli, replay/raw, io, server/config, server/registry."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. protocols.py — 0% — just import the module and classes
# ---------------------------------------------------------------------------


class TestProtocols:
    def test_import_terminal_reader(self) -> None:
        from provide.terminal.protocols import TerminalReader

        assert TerminalReader is not None

    def test_import_terminal_writer(self) -> None:
        from provide.terminal.protocols import TerminalWriter

        assert TerminalWriter is not None


# ---------------------------------------------------------------------------
# 2. __init__.py lines 91-93 — _SERVER_EXPORTS branch
# ---------------------------------------------------------------------------


class TestInitServerExports:
    def test_create_server_app_accessible_via_init(self) -> None:
        import provide.terminal as ut

        fn = ut.create_server_app
        assert callable(fn)

    def test_load_server_config_accessible_via_init(self) -> None:
        import provide.terminal as ut

        fn = ut.load_server_config
        assert callable(fn)

    def test_default_server_config_accessible_via_init(self) -> None:
        import provide.terminal as ut

        fn = ut.default_server_config
        assert callable(fn)


# ---------------------------------------------------------------------------
# 3. screen.py — except re.error branches
# ---------------------------------------------------------------------------


class TestScreenRegexErrors:
    def test_extract_menu_options_invalid_regex(self) -> None:
        from provide.terminal.screen import extract_menu_options

        # Invalid regex — should return empty list (except re.error branch)
        result = extract_menu_options("some screen text", pattern="[invalid(")
        assert result == []

    def test_extract_numbered_list_invalid_regex(self) -> None:
        from provide.terminal.screen import extract_numbered_list

        result = extract_numbered_list("1. Item one\n2. Item two", pattern="[invalid(")
        assert result == []

    def test_extract_key_value_pairs_invalid_regex(self) -> None:
        from provide.terminal.screen import extract_key_value_pairs

        result = extract_key_value_pairs("Credits: 1000", {"credits": "[invalid("})
        assert result == {}

    def test_extract_key_value_pairs_mixed_valid_invalid(self) -> None:
        from provide.terminal.screen import extract_key_value_pairs

        # One valid, one invalid — valid should succeed
        result = extract_key_value_pairs(
            "Credits: 1000 Sector: 42",
            {"credits": r"Credits:\s*(\d+)", "bad": "[invalid("},
        )
        assert result.get("credits") == "1000"
        assert "bad" not in result

    def test_extract_menu_options_valid_pattern(self) -> None:
        from provide.terminal.screen import extract_menu_options

        # Ensure normal path still works
        result = extract_menu_options("[A] Attack  [D] Defend", None)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# 4. replay/viewer.py lines 57-59 — except json.JSONDecodeError
# ---------------------------------------------------------------------------


class TestReplayViewerJsonError:
    def test_replay_log_skips_corrupt_lines(self) -> None:
        from provide.terminal.replay.viewer import replay_log

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            # corrupt line
            f.write("this is not json\n")
            # valid line with wrong event type — will be skipped anyway
            f.write(json.dumps({"event": "other", "data": {}, "ts": 1.0}) + "\n")
            tmp_path = f.name

        import io

        output = io.StringIO()
        # Should not raise; corrupt line triggers the JSONDecodeError handler
        replay_log(tmp_path, output=output, speed=1.0, step=False)
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. session_logger.py — various branches
# ---------------------------------------------------------------------------


class TestSessionLoggerBranches:
    async def test_start_exception_closes_file_and_reraises(self) -> None:
        """Lines 57-60: except Exception in start() closes file and re-raises."""
        from provide.terminal.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)

            # Mock _write_event_unlocked to raise
            with (
                patch.object(sl, "_write_event_unlocked", side_effect=OSError("boom")),
                pytest.raises(OSError, match="boom"),
            ):
                await sl.start("sess1")

            # File should be closed and set to None after exception
            assert sl._file is None

    async def test_stop_without_start_is_noop(self) -> None:
        """Lines 67->73 and 73->exit: stop() when _file is None — both False branches."""
        from provide.terminal.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)
            # Never called start(), so _file is None
            await sl.stop()  # should not raise

    async def test_stop_twice_second_is_noop(self) -> None:
        """Line 73->exit: second stop() call hits file_to_close is None branch."""
        from provide.terminal.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)
            await sl.start("sess1")
            await sl.stop()
            # Second call: _file is already None → file_to_close is None
            await sl.stop()

    async def test_write_event_with_context(self) -> None:
        """Lines 144->146: context is set, so record['ctx'] is included."""
        from provide.terminal.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)
            await sl.start("sess1")
            sl.set_context({"user": "alice", "system": "prod"})
            await sl.log_send("hello")
            await sl.stop()

            lines = log_path.read_text(encoding="utf-8").splitlines()
            # find a line with "send" event
            send_lines = [ln for ln in lines if '"send"' in ln]
            assert send_lines, "Expected at least one send event"
            record = json.loads(send_lines[0])
            assert "ctx" in record
            assert record["ctx"]["user"] == "alice"


# ---------------------------------------------------------------------------
# 6. hijack/models.py line 114->116 — extract_prompt_id falsy value
# ---------------------------------------------------------------------------


class TestExtractPromptId:
    def test_empty_string_prompt_id_returns_none(self) -> None:
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": ""}}
        assert extract_prompt_id(snapshot) is None

    def test_non_string_prompt_id_returns_none(self) -> None:
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": 42}}
        assert extract_prompt_id(snapshot) is None

    def test_none_prompt_id_returns_none(self) -> None:
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": None}}
        assert extract_prompt_id(snapshot) is None

    def test_valid_prompt_id_returned(self) -> None:
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": "menu_main"}}
        assert extract_prompt_id(snapshot) == "menu_main"


# ---------------------------------------------------------------------------
# 7. hijack/hub/polling.py line 106->109 — no new snapshot since last poll
# ---------------------------------------------------------------------------


class TestWaitForGuardNoNewSnapshot:
    async def test_snap_ts_not_advanced_triggers_request_snapshot(self) -> None:
        """Line 106->109: snap_ts <= last_snap_ts → request_snapshot called again."""
        from provide.terminal.bridge.hub import TermHub
        from provide.terminal.bridge.models import WorkerTermState

        hub = TermHub()

        # Register a worker with a snapshot that has an old timestamp
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()
            # Set a snapshot with ts=1.0 (old, won't match future time)
            st.last_snapshot = {"screen": "no match here", "ts": 1.0}

        request_count = 0
        original_req = hub.request_snapshot

        async def counting_req(wid: str) -> None:
            nonlocal request_count
            request_count += 1
            await original_req(wid)

        hub.request_snapshot = counting_req  # type: ignore[method-assign]

        # Run wait_for_guard with an expect_regex that won't match.
        # Use a very short timeout so it exits quickly.
        matched, snap, reason = await hub.wait_for_guard(
            "w1",
            expect_prompt_id=None,
            expect_regex="NEVER_MATCH_12345",
            timeout_ms=150,
            poll_interval_ms=30,
        )

        assert matched is False
        # request_snapshot should have been called multiple times (initial + retries when ts <= last)
        assert request_count >= 2


# ---------------------------------------------------------------------------
# 8. hijack/base.py — watchdog and stop_watchdog branches
# ---------------------------------------------------------------------------


class TestHijackableMixinBranches:
    async def test_watchdog_fires_with_on_stuck_none(self) -> None:
        """Lines 146->152: on_stuck is None, watchdog fires but does not call it."""
        from provide.terminal.bridge.base import HijackableMixin

        class MyWorker(HijackableMixin):
            pass

        worker = MyWorker()
        # Start watchdog with on_stuck=None and tiny timeouts
        worker.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.05, on_stuck=None)
        # Wait for the watchdog to fire at least once
        await asyncio.sleep(0.2)
        await worker.stop_watchdog()
        # No assertion needed — just ensure no error raised

    async def test_stop_watchdog_before_start_is_noop(self) -> None:
        """Line 163->exit: stop_watchdog() when _watchdog_task is None."""
        from provide.terminal.bridge.base import HijackableMixin

        class MyWorker(HijackableMixin):
            pass

        worker = MyWorker()
        assert worker._watchdog_task is None
        # Should not raise
        await worker.stop_watchdog()


# ---------------------------------------------------------------------------
# 9. ansi.py — _handle_tilde_codes and _handle_brace_tokens
# ---------------------------------------------------------------------------


class TestAnsiCoverage:
    def test_tilde_code_not_in_tilde_map_falls_through(self) -> None:
        """Line 326->333: code not in _TILDE_MAP → out.append(text[i])."""
        from provide.terminal.ansi import preview_ansi

        # '~z' — 'z' is not in _TILDE_MAP, so both ~ and z are appended as-is
        result = preview_ansi("hello~zworld")
        assert "~z" in result or "~" in result

    def test_tilde_code_emit_color_returns_empty(self) -> None:
        """Line 329->333: seq is empty (invalid color char via direct call)."""
        from provide.terminal.ansi import _emit_color, _handle_tilde_codes

        # Directly verify _emit_color returns "" for unknown color char
        assert _emit_color("+", "z") == ""

        # For _handle_tilde_codes, we need a tilde code whose _emit_color returns ""
        # The _TILDE_MAP only maps to known color chars, so we test directly
        # by checking that the function handles seq="" without appending
        # We can't do it via preview_ansi since all _TILDE_MAP entries are valid.
        # Call _handle_tilde_codes with a custom text that simulates seq=""
        # by monkeypatching _emit_color

        with patch("provide.terminal.ansi._emit_color", return_value=""):
            result = _handle_tilde_codes("~1text")
        # When seq is empty (""), the continue is skipped, so "~" and "1" are appended normally
        assert "~" in result or "1" in result

    def test_brace_token_polarity_not_plus_or_minus_falls_through(self) -> None:
        """Line 345->351: polarity not in ('+', '-') → fall through to out.append."""
        from provide.terminal.ansi import _handle_brace_tokens

        # '{x+g}' — polarity 'x' is not '+' or '-' → falls through
        result = _handle_brace_tokens("{x+g}rest")
        assert "{" in result  # not converted, kept as-is

    def test_brace_token_emit_color_empty_falls_through(self) -> None:
        """Line 348->351: seq is empty → the continue is skipped."""
        from provide.terminal.ansi import _handle_brace_tokens

        # An unrecognised brace sequence falls through: '{' is emitted literally.
        result = _handle_brace_tokens("{??}rest")
        assert "{" in result


# (TestEmulatorCachedSnapshot, TestCliSSHTransportMissing, TestReplayRawBranch,
#  TestServerConfigRelativeDir, TestRegistryRuntimeStop, TestIoBranches
#  moved to test_misc_coverage_2.py)


class TestLazySessionExports:
    """Test lazy import of TelnetSession / connect_telnet via __getattr__ (lines 107-109)."""

    def test_lazy_import_telnet_session(self) -> None:
        import provide.terminal

        cls = provide.terminal.TelnetSession
        from provide.terminal.telnet_session import TelnetSession

        assert cls is TelnetSession

    def test_lazy_import_connect_telnet(self) -> None:
        import provide.terminal

        fn = provide.terminal.connect_telnet
        from provide.terminal.telnet_session import connect_telnet

        assert fn is connect_telnet
