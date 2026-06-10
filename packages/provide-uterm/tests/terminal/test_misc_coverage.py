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
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. protocols.py — 0% — just import the module and classes
# ---------------------------------------------------------------------------


class TestProtocols:
    def test_import_terminal_reader(self) -> None:
        from provide.uterm.protocols import TerminalReader

        assert TerminalReader is not None

    def test_import_terminal_writer(self) -> None:
        from provide.uterm.protocols import TerminalWriter

        assert TerminalWriter is not None


# ---------------------------------------------------------------------------
# 2. screen.py — except re.error branches
# ---------------------------------------------------------------------------


class TestScreenRegexErrors:
    def test_extract_menu_options_invalid_regex(self) -> None:
        from provide.uterm.screen import extract_menu_options

        # Invalid regex — should return empty list (except re.error branch)
        result = extract_menu_options("some screen text", pattern="[invalid(")
        assert result == []

    def test_extract_numbered_list_invalid_regex(self) -> None:
        from provide.uterm.screen import extract_numbered_list

        result = extract_numbered_list("1. Item one\n2. Item two", pattern="[invalid(")
        assert result == []

    def test_extract_key_value_pairs_invalid_regex(self) -> None:
        from provide.uterm.screen import extract_key_value_pairs

        result = extract_key_value_pairs("Credits: 1000", {"credits": "[invalid("})
        assert result == {}

    def test_extract_key_value_pairs_mixed_valid_invalid(self) -> None:
        from provide.uterm.screen import extract_key_value_pairs

        # One valid, one invalid — valid should succeed
        result = extract_key_value_pairs(
            "Credits: 1000 Sector: 42",
            {"credits": r"Credits:\s*(\d+)", "bad": "[invalid("},
        )
        assert result.get("credits") == "1000"
        assert "bad" not in result

    def test_extract_menu_options_valid_pattern(self) -> None:
        from provide.uterm.screen import extract_menu_options

        # Ensure normal path still works
        result = extract_menu_options("[A] Attack  [D] Defend", None)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# 4. replay/viewer.py lines 57-59 — except json.JSONDecodeError
# ---------------------------------------------------------------------------


class TestReplayViewerJsonError:
    def test_replay_log_skips_corrupt_lines(self) -> None:
        from provide.uterm.replay.viewer import replay_log

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
        from provide.uterm.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)

            # Mock store.start_session to raise
            with (
                patch.object(sl._store, "start_session", side_effect=OSError("boom")),
                pytest.raises(OSError, match="boom"),
            ):
                await sl.start("sess1")

            # File should be closed and set to None after exception
            assert sl._file is None

    async def test_stop_without_start_is_noop(self) -> None:
        """Lines 67->73 and 73->exit: stop() when _file is None — both False branches."""
        from provide.uterm.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)
            # Never called start(), so _file is None
            await sl.stop()  # should not raise

    async def test_stop_twice_second_is_noop(self) -> None:
        """Line 73->exit: second stop() call hits file_to_close is None branch."""
        from provide.uterm.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            sl = SessionLogger(log_path)
            await sl.start("sess1")
            await sl.stop()
            # Second call: _file is already None → file_to_close is None
            await sl.stop()

    async def test_write_event_with_context(self) -> None:
        """Lines 144->146: context is set, so record['ctx'] is included."""
        from provide.uterm.session_logger import SessionLogger

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
        from provide.uterm.server.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": ""}}
        assert extract_prompt_id(snapshot) is None

    def test_non_string_prompt_id_returns_none(self) -> None:
        from provide.uterm.server.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": 42}}
        assert extract_prompt_id(snapshot) is None

    def test_none_prompt_id_returns_none(self) -> None:
        from provide.uterm.server.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": None}}
        assert extract_prompt_id(snapshot) is None

    def test_valid_prompt_id_returned(self) -> None:
        from provide.uterm.server.bridge.rest_helpers import extract_prompt_id

        snapshot = {"prompt_detected": {"prompt_id": "menu_main"}}
        assert extract_prompt_id(snapshot) == "menu_main"


# ---------------------------------------------------------------------------
# 7. hub/polling_service.py line 161->163 — no new snapshot since last poll
# ---------------------------------------------------------------------------


class TestWaitForGuardNoNewSnapshot:
    async def test_snap_ts_not_advanced_triggers_request_snapshot(self) -> None:
        """polling_service 161->163: snap_ts <= last_snap_ts → request_snapshot again.

        Deterministic by construction: the poll loop's monotonic clock is mocked
        so the loop runs *exactly two* iterations regardless of host speed, and
        ``asyncio.sleep`` is neutralised. The retry branch only fires from the
        second iteration (the first poll sets ``last_snap_ts`` from 0.0 → snap_ts),
        so a healthy retry yields exactly one extra request on top of the initial.

        The prior version relied on wall-clock time — ``timeout_ms=150`` had to
        fit at least two real 30ms polls — and flaked on starved CI runners where
        only one iteration ran before the deadline (``request_count == 1``).
        """
        import types

        from provide.uterm.server.bridge.hub import TermHub
        from provide.uterm.server.bridge.models import WorkerTermState

        hub = TermHub()

        # Worker snapshot whose ts never advances, so every poll after the first
        # sees snap_ts <= last_snap_ts and must re-request.
        async with hub._lock:
            st = hub.registry._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = AsyncMock()
            st.worker_ws.send_text = AsyncMock()
            st.last_snapshot = {"screen": "no match here", "ts": 1.0}

        request_count = 0
        original_req = hub.request_snapshot

        async def counting_req(wid: str) -> None:
            nonlocal request_count
            request_count += 1
            await original_req(wid)

        hub.request_snapshot = counting_req  # type: ignore[method-assign]

        # Pin the loop to exactly two iterations: the first monotonic() sets
        # end = 0.0 + timeout; the next two ticks (0.0, 0.001) are < end so two
        # iterations run, then the exhausted iterator yields 999.0 to exit. With
        # sleep no-op'd, no real time elapses, so host speed can't change the count.
        ticks = iter([0.0, 0.0, 0.001])
        fake_time = types.SimpleNamespace(monotonic=lambda: next(ticks, 999.0))
        ps = "provide.uterm.server.bridge.hub.polling_service"
        with patch(f"{ps}.time", fake_time), patch(f"{ps}.asyncio.sleep", AsyncMock()):
            matched, _snap, reason = await hub.wait_for_guard(
                "w1",
                expect_prompt_id=None,
                expect_regex="NEVER_MATCH_12345",
                timeout_ms=150,
                poll_interval_ms=30,
            )

        assert matched is False
        assert reason == "prompt_guard_not_satisfied"
        # 1 initial request + exactly 1 retry on the second poll (ts did not advance).
        assert request_count == 2


# ---------------------------------------------------------------------------
# 8. hijack/base.py — watchdog and stop_watchdog branches
# ---------------------------------------------------------------------------


class TestHijackableMixinBranches:
    @staticmethod
    def _worker() -> Any:
        from provide.uterm.bridge.base import HijackableMixin

        class MyWorker(HijackableMixin):
            pass

        return MyWorker()

    # -- await_if_hijacked: the three exit paths -------------------------------

    async def test_await_if_hijacked_returns_when_not_hijacked(self) -> None:
        """Line 66-67: not hijacked → return immediately."""
        worker = self._worker()
        await asyncio.wait_for(worker.await_if_hijacked(), timeout=1.0)

    async def test_await_if_hijacked_consumes_a_step_token(self) -> None:
        """Line 68-70: a step token is available → pass without blocking, decrement."""
        worker = self._worker()
        await worker.set_hijacked(True)
        await worker.request_step(1)
        await asyncio.wait_for(worker.await_if_hijacked(), timeout=1.0)
        assert worker._hijack_step_tokens == 0

    async def test_await_if_hijacked_blocks_until_resumed(self) -> None:
        """Line 71: hijacked, no tokens → block on the event until resumed."""
        worker = self._worker()
        await worker.set_hijacked(True)
        task = asyncio.create_task(worker.await_if_hijacked())
        await asyncio.sleep(0.02)
        assert not task.done()  # blocked on the cleared event
        await worker.set_hijacked(False)  # resume → event set
        await asyncio.wait_for(task, timeout=1.0)

    # -- set_hijacked: idempotent / enable / disable --------------------------

    async def test_set_hijacked_is_idempotent(self) -> None:
        """Line 78-79: setting the same value is a no-op."""
        worker = self._worker()
        await worker.set_hijacked(False)  # already not hijacked
        assert worker._hijacked is False
        await worker.set_hijacked(True)
        await worker.set_hijacked(True)  # same value → no-op
        assert worker._hijacked is True
        assert not worker._hijack_event.is_set()  # enable cleared the event

    # -- request_step: no-op / accumulate + cap -------------------------------

    async def test_request_step_is_noop_when_not_hijacked(self) -> None:
        """Line 95-96: not hijacked → no tokens granted."""
        worker = self._worker()
        await worker.request_step(5)
        assert worker._hijack_step_tokens == 0

    async def test_request_step_accumulates_and_caps_at_100(self) -> None:
        """Line 97: tokens accumulate but are capped at 100."""
        worker = self._worker()
        await worker.set_hijacked(True)
        await worker.request_step(2)
        assert worker._hijack_step_tokens == 2
        await worker.request_step(200)
        assert worker._hijack_step_tokens == 100

    # -- start_watchdog: idempotency + loop-body branches ---------------------
    # The loop floors its sleep at max(0.5, check_interval_s), so the body only
    # runs after ~0.5s — every watchdog-body test waits past that floor.

    async def test_start_watchdog_is_idempotent_while_running(self) -> None:
        """Line 133-134: a second start while running returns the same task."""
        worker = self._worker()
        worker.start_watchdog(stuck_timeout_s=10.0, check_interval_s=10.0)
        first = worker._watchdog_task
        worker.start_watchdog(stuck_timeout_s=10.0, check_interval_s=10.0)
        assert worker._watchdog_task is first
        await worker.stop_watchdog()

    async def test_watchdog_fires_with_on_stuck_none(self) -> None:
        """Line 146->152: idle exceeds the timeout, on_stuck is None → no callback."""
        worker = self._worker()
        worker._last_progress_mono = time.monotonic() - 1000.0
        worker.start_watchdog(stuck_timeout_s=0.0, check_interval_s=0.0, on_stuck=None)
        await asyncio.sleep(0.7)  # > the 0.5s loop-sleep floor → ≥1 iteration
        await worker.stop_watchdog()

    async def test_watchdog_calls_on_stuck_when_idle(self) -> None:
        """Line 146-148: idle exceeds the timeout → the on_stuck callback runs."""
        worker = self._worker()
        on_stuck = AsyncMock()
        worker._last_progress_mono = time.monotonic() - 1000.0
        worker.start_watchdog(stuck_timeout_s=0.0, check_interval_s=0.0, on_stuck=on_stuck)
        await asyncio.sleep(0.7)
        await worker.stop_watchdog()
        on_stuck.assert_awaited()

    async def test_watchdog_continues_when_within_timeout(self) -> None:
        """Line 143-145: idle is below the timeout → the loop continues, no fire."""
        worker = self._worker()
        on_stuck = AsyncMock()
        worker.note_progress()  # fresh progress
        worker.start_watchdog(stuck_timeout_s=100.0, check_interval_s=0.0, on_stuck=on_stuck)
        await asyncio.sleep(0.7)
        await worker.stop_watchdog()
        on_stuck.assert_not_awaited()

    async def test_watchdog_is_suppressed_while_hijacked(self) -> None:
        """Line 140-142: while hijacked the watchdog self-defers (never fires)."""
        worker = self._worker()
        on_stuck = AsyncMock()
        await worker.set_hijacked(True)
        worker._last_progress_mono = time.monotonic() - 1000.0
        worker.start_watchdog(stuck_timeout_s=0.0, check_interval_s=0.0, on_stuck=on_stuck)
        await asyncio.sleep(0.7)
        await worker.stop_watchdog()
        on_stuck.assert_not_awaited()

    # -- stop_watchdog / cleanup ----------------------------------------------

    async def test_stop_watchdog_before_start_is_noop(self) -> None:
        """Line 163->exit: stop_watchdog() when _watchdog_task is None."""
        worker = self._worker()
        assert worker._watchdog_task is None
        await worker.stop_watchdog()  # should not raise

    async def test_cleanup_hijack_resets_and_stops(self) -> None:
        """Line 169-172: cleanup resumes automation and cancels the watchdog."""
        worker = self._worker()
        await worker.set_hijacked(True)
        worker.start_watchdog(stuck_timeout_s=10.0, check_interval_s=10.0)
        await worker.cleanup_hijack()
        assert worker._hijacked is False
        assert worker._watchdog_task is None


# ---------------------------------------------------------------------------
# 9. ansi.py — _handle_tilde_codes and _handle_brace_tokens
# ---------------------------------------------------------------------------


class TestAnsiCoverage:
    def test_tilde_code_not_in_tilde_map_falls_through(self) -> None:
        """Line 326->333: code not in _TILDE_MAP → out.append(text[i])."""
        from provide.uterm.ansi import preview_ansi

        # '~z' — 'z' is not in _TILDE_MAP, so both ~ and z are appended as-is
        result = preview_ansi("hello~zworld")
        assert "~z" in result or "~" in result

    def test_tilde_code_emit_color_returns_empty(self) -> None:
        """Line 329->333: seq is empty (invalid color char via direct call)."""
        from provide.uterm.ansi import _emit_color, _handle_tilde_codes

        # Directly verify _emit_color returns "" for unknown color char
        assert _emit_color("+", "z") == ""

        # For _handle_tilde_codes, we need a tilde code whose _emit_color returns ""
        # The _TILDE_MAP only maps to known color chars, so we test directly
        # by checking that the function handles seq="" without appending
        # We can't do it via preview_ansi since all _TILDE_MAP entries are valid.
        # Call _handle_tilde_codes with a custom text that simulates seq=""
        # by monkeypatching _emit_color

        with patch("provide.uterm.ansi._emit_color", return_value=""):
            result = _handle_tilde_codes("~1text")
        # When seq is empty (""), the continue is skipped, so "~" and "1" are appended normally
        assert "~" in result or "1" in result

    def test_brace_token_polarity_not_plus_or_minus_falls_through(self) -> None:
        """Line 345->351: polarity not in ('+', '-') → fall through to out.append."""
        from provide.uterm.ansi import _handle_brace_tokens

        # '{x+g}' — polarity 'x' is not '+' or '-' → falls through
        result = _handle_brace_tokens("{x+g}rest")
        assert "{" in result  # not converted, kept as-is

    def test_brace_token_emit_color_empty_falls_through(self) -> None:
        """Line 348->351: seq is empty → the continue is skipped."""
        from provide.uterm.ansi import _handle_brace_tokens

        # An unrecognised brace sequence falls through: '{' is emitted literally.
        result = _handle_brace_tokens("{??}rest")
        assert "{" in result


# (TestEmulatorCachedSnapshot, TestCliSSHTransportMissing, TestReplayRawBranch,
#  TestServerConfigRelativeDir, TestRegistryRuntimeStop, TestIoBranches
#  moved to test_misc_coverage_2.py)
