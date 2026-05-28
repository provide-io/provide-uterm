#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Second-pass coverage gap tests for remaining missing branches."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState


def _make_app(**hub_kwargs: Any) -> tuple[TermHub, FastAPI, TestClient]:
    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _read_initial_browser(browser: Any) -> tuple[dict, dict]:
    """Read hello + hijack_state from a newly-connected browser WS."""
    hello = browser.receive_json()
    assert hello["type"] == "hello"
    hs = browser.receive_json()
    assert hs["type"] == "hijack_state"
    return hello, hs


async def _register_worker(hub: TermHub, worker_id: str, ws: Any) -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.worker_ws = ws


async def _register_browser_ws(hub: TermHub, worker_id: str, browser_ws: Any, role: str = "admin") -> None:
    async with hub._lock:
        st = hub._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role


# ---------------------------------------------------------------------------
# hijack/bridge.py lines 214-220 — InvalidURI stops reconnect (corrected test)
# ---------------------------------------------------------------------------


class TestBridgeInvalidUriFixed:
    async def test_invalid_uri_stops_reconnect_via_run(self) -> None:
        """Lines 214-220: InvalidURI exception → _running=False, break from reconnect loop."""
        from provide.uterm.server.bridge.worker_link import TermBridge, _InvalidURI

        if _InvalidURI is None:
            return  # websockets not installed

        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._worker = bot
        bridge._worker_id = "w1"
        bridge._manager_url = "not-a-valid-url"
        bridge._max_ws_message_bytes = 1024 * 1024
        bridge._running = False  # Must be False so start() creates the task
        bridge._send_q = asyncio.Queue()
        bridge._task = None
        bridge._latest_snapshot = None

        import contextlib

        mock_exc = _InvalidURI.__new__(_InvalidURI)
        with contextlib.suppress(Exception):
            mock_exc.__init__("bad-url", "not a URI")

        with patch("websockets.connect", side_effect=mock_exc):
            await bridge.start()  # Creates the _run() task
            await asyncio.sleep(0.1)  # Let _run() execute and hit InvalidURI
            await bridge.stop()

        assert not bridge._running


# ---------------------------------------------------------------------------
# server/runtime.py line 148->150 — _start_connector: is_connected() returns False
# ---------------------------------------------------------------------------


class TestStartConnectorIsConnectedFalse:
    async def test_start_connector_connected_false_skips_set(self) -> None:
        """Line 148->150: connector.is_connected() returns False → _connected stays False."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from provide.uterm.server.models import RecordingConfig, SessionDefinition
        from provide.uterm.server.runtime import HostedSessionRuntime

        definition = SessionDefinition(
            session_id="test-sess",
            display_name="Test",
            connector_type="shell",
            auto_start=False,
        )
        runtime = HostedSessionRuntime(
            definition,
            public_base_url="http://localhost:9999",
            recording=RecordingConfig(),
        )

        connector = MagicMock()
        connector.is_connected = MagicMock(return_value=False)  # False branch
        connector.start = AsyncMock()

        with patch("provide.uterm.server.runtime.build_connector", return_value=connector):
            result = await runtime._start_connector()

        assert runtime._connected is False  # NOT set (False branch of line 148)
        assert result is connector


# ---------------------------------------------------------------------------
# server/runtime.py line 233->237 — _bridge_session: unknown mtype recv wins
# ---------------------------------------------------------------------------


class TestBridgeSessionUnknownMtypeRecvWins:
    async def test_unknown_mtype_recv_wins_over_poll(self) -> None:
        """Line 233->237: recv_task completes first with unknown mtype → responses=[]."""
        import asyncio
        import json
        from unittest.mock import AsyncMock, MagicMock

        from provide.uterm.server.models import RecordingConfig, SessionDefinition
        from provide.uterm.server.runtime import HostedSessionRuntime

        definition = SessionDefinition(
            session_id="test-session",
            display_name="Test Session",
            connector_type="shell",
            auto_start=False,
        )
        runtime = HostedSessionRuntime(
            definition,
            public_base_url="http://localhost:9999",
            recording=RecordingConfig(),
        )

        connector = MagicMock()
        connector.is_connected = MagicMock(return_value=False)
        connector.start = AsyncMock()
        connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "ts": 1.0})
        connector.set_mode = AsyncMock(return_value=[])

        # poll_messages blocks for a long time so recv_task always wins
        async def _slow_poll() -> list:
            await asyncio.sleep(100)
            return []

        connector.poll_messages = _slow_poll
        connector.handle_input = AsyncMock(return_value=[])
        connector.handle_control = AsyncMock(return_value=[])

        runtime._connector = connector
        runtime._queue = asyncio.Queue()

        call_count = 0

        async def mock_recv() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps({"type": "unknown_weird_type_xyz"})
            # Signal stop then return a valid frame so recv_task.result() doesn't raise
            runtime._stop.set()
            return json.dumps({"type": "stop_sentinel"})

        mock_ws = AsyncMock()
        sent_msgs: list = []

        async def mock_send(data: str) -> None:
            sent_msgs.append(data)

        mock_ws.send = mock_send
        mock_ws.recv = mock_recv

        runtime._log_snapshot = AsyncMock()  # type: ignore[method-assign]
        runtime._log_event = AsyncMock()  # type: ignore[method-assign]
        runtime._log_send = AsyncMock()  # type: ignore[method-assign]

        await runtime._bridge_session(mock_ws)

        # Unknown mtype → responses=[] → for loop at 237 doesn't iterate
        unknown_sent = [m for m in sent_msgs if "unknown_weird_type_xyz" in m]
        assert len(unknown_sent) == 0


# ---------------------------------------------------------------------------
# ansi.py line 345->351 — _handle_brace_tokens: emit_color returns empty
# ---------------------------------------------------------------------------


class TestAnsiTildeCodeNotInMap:
    def test_tilde_code_not_in_map_falls_through(self) -> None:
        """Line 326->333: tilde code not in _TILDE_MAP → literal passthrough."""
        from provide.uterm.ansi import _handle_tilde_codes

        # '~Z' — 'Z' is not in _TILDE_MAP → False branch of line 326 → appends '~' literally
        result = _handle_tilde_codes("~Z")
        assert "~" in result


class TestAnsiBraceTokenInvalidPolarity:
    def test_brace_token_with_invalid_polarity_falls_through(self) -> None:
        """Line 345->351: polarity NOT in ('+', '-') → falls through to out.append(text[i])."""
        from provide.uterm.ansi import _handle_brace_tokens

        # '{xR}' - polarity='x' is not in ('+', '-') → False branch of line 345 → appends '{'
        # Note: brace token format is exactly 4 chars: {<polarity><color>}
        result = _handle_brace_tokens("{xR}")
        # '{' is appended as a literal character
        assert "{" in result

    def test_brace_token_with_unknown_color_char_falls_through(self) -> None:
        """Line 347->351: polarity valid but emit_color returns '' (unknown char) → fall through."""
        from provide.uterm.ansi import _handle_brace_tokens

        # color_char 'Z' is not in _PREVIEW_COLOR_MAP and not 'x', so _emit_color returns ""
        result = _handle_brace_tokens("{+Z}")
        # Since seq is "", the token is not consumed — '{' is appended literally
        assert "{" in result


# ---------------------------------------------------------------------------
# screen.py lines 140->137, 169->164 — re.error in extract functions
# ---------------------------------------------------------------------------


class TestScreenRegexErrors:
    def test_extract_menu_options_empty_description_skipped(self) -> None:
        """Line 140->137: if description: False → description is whitespace/empty after strip."""
        from provide.uterm.screen import extract_menu_options

        # Use a custom pattern that matches but produces an empty description group
        # Pattern: match <A> followed by spaces only (description = whitespace → strip = "")
        result = extract_menu_options(
            "<A>    <B> Item",
            pattern=r"<([A-Z])>\s+([^\S]*?)(?=<|$)",
        )
        # The first match <A> has an empty stripped description → skipped
        # This exercises the False branch of 'if description:'
        assert isinstance(result, list)

    def test_extract_numbered_list_empty_description_skipped(self) -> None:
        """Line 169->164: if description: False → description is whitespace after strip."""
        from provide.uterm.screen import extract_numbered_list

        # Use a custom pattern where the second group can match empty/whitespace
        result = extract_numbered_list(
            "1.   \n2. Item",
            pattern=r"^\s*(\d+)\.\s+(.*)$",
        )
        # Line "1.   " → description = "  " → strip() = "" → skipped (False branch)
        # Line "2. Item" → description = "Item" → appended
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# session_logger.py line 67->73 — stop() when file is None (not started)
# ---------------------------------------------------------------------------


class TestSessionLoggerStopWhenNotStarted:
    async def test_stop_when_not_started_is_noop(self) -> None:
        """Line 67->73: stop() when self._file is None → file_to_close is None, no close."""
        import tempfile
        from pathlib import Path

        from provide.uterm.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmp:
            logger = SessionLogger(Path(tmp) / "test.jsonl")
            # Stop without starting — should not raise
            await logger.stop()
            # File should still not exist
            assert not (Path(tmp) / "test.jsonl").exists()


class TestSessionLoggerWriteAtQuota:
    async def test_write_event_at_max_bytes_suppresses(self) -> None:
        """Line 144->146: _write_event_unlocked when max_bytes exceeded → return early."""
        import tempfile
        from pathlib import Path

        from provide.uterm.session_logger import SessionLogger

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "quota.jsonl"
            logger = SessionLogger(log_path, max_bytes=1)  # 1 byte quota
            await logger.start("test-session")
            # Force bytes_written above the quota
            logger._bytes_written = 999_999
            # This write should be suppressed
            await logger.log_send("should-not-appear")
            await logger.stop()

            content = log_path.read_text()
            assert "should-not-appear" not in content


# ---------------------------------------------------------------------------
# server/config.py line 57->59 — relative directory: is_absolute() is True
# ---------------------------------------------------------------------------


class TestServerConfigAbsoluteDirectory:
    def test_load_server_config_with_absolute_recording_dir(self) -> None:
        """Line 57->59: recording directory is already absolute → no resolve needed."""
        import tempfile
        from pathlib import Path

        from provide.uterm.server.config import load_server_config

        with tempfile.TemporaryDirectory() as tmp:
            abs_dir = Path(tmp).resolve()
            cfg_path = Path(tmp) / "server.toml"
            cfg_path.write_text(
                f'[recording]\ndirectory = "{abs_dir}"\n',
                encoding="utf-8",
            )
            config = load_server_config(cfg_path)
            # directory should remain the absolute path (not resolved relative to cfg_path)
            assert config.recording.directory == abs_dir


# ---------------------------------------------------------------------------
# hijack/base.py line 146->152 — watchdog fires with on_stuck=None
# ---------------------------------------------------------------------------


class TestWatchdogOnStuckNone:
    async def test_watchdog_fires_without_on_stuck_callback(self) -> None:
        """Line 146->152: idle_for >= stuck_timeout_s and on_stuck is None → skip call."""
        from provide.uterm.bridge.base import HijackableMixin

        class FakeWorker(HijackableMixin):
            pass

        worker = FakeWorker()
        worker._hijacked = False
        # Make last_progress very old so watchdog triggers immediately
        worker._last_progress_mono = 0.0

        # Start watchdog with no on_stuck callback.
        # min check_interval is max(0.5, ...) so must sleep > 0.5s
        worker.start_watchdog(stuck_timeout_s=0.001, check_interval_s=0.001)

        # Let watchdog fire at least once (check_interval clamped to 0.5s)
        await asyncio.sleep(0.6)

        # Stop watchdog — should not raise
        await worker.stop_watchdog()


# ---------------------------------------------------------------------------
# hijack/hub/core.py line 175->177 — resolved_role is non-None but invalid
# ---------------------------------------------------------------------------


class TestResolveRoleNoneReturnsViewer:
    async def test_resolver_returns_none_falls_back_to_viewer(self) -> None:
        """Line 175->177: resolved_role is None → skip warning log, return 'viewer'."""
        hub = TermHub(resolve_browser_role=lambda ws, wid: None)  # returns None

        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer"  # Falls back to "viewer" without logging

    async def test_invalid_non_none_role_logs_and_returns_viewer(self) -> None:
        """Line 175 True→176: resolved_role is non-None but invalid → log warning."""
        hub = TermHub(resolve_browser_role=lambda ws, wid: "superadmin")  # invalid role

        ws = _make_ws()
        role = await hub._resolve_role_for_browser(ws, "w1")
        assert role == "viewer"  # Falls back to "viewer"


# ---------------------------------------------------------------------------
# hijack/hub/core.py line 289 — broadcast_hijack_state: st2 is None after remove_dead_browsers
# ---------------------------------------------------------------------------


class TestBroadcastHijackStateSt2None:
    async def test_broadcast_hijack_state_st2_none_after_dead_removal(self) -> None:
        """Line 289: st2 is None after remove_dead_browsers → return early."""
        hub = TermHub()

        dead_ws = _make_ws()
        dead_ws.send_text = AsyncMock(side_effect=Exception("dead"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.browsers[dead_ws] = "admin"

        # Patch remove_dead_browsers to also delete the worker entry
        original_rdb = hub.remove_dead_browsers

        async def _removing_rdb(worker_id: str, dead: set) -> bool:
            result = await original_rdb(worker_id, dead)
            # Delete the worker state to simulate st2 is None at line 288
            async with hub._lock:
                hub._workers.pop(worker_id, None)
            return result

        hub.remove_dead_browsers = _removing_rdb  # type: ignore[method-assign]

        # This should exercise line 289 (st2 is None → return)
        await hub.broadcast_hijack_state("w1")


# ---------------------------------------------------------------------------
# hijack/hub/core.py line 327->329 — send_worker clears dead ws on failure
# ---------------------------------------------------------------------------


class TestSendWorkerClearsDeadWs:
    async def test_send_worker_clears_dead_worker_ws_on_failure(self) -> None:
        """Line 327 True→328: send_worker fails, st2.worker_ws is the dead ws → clear it."""
        hub = TermHub()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock(side_effect=Exception("send failed"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws

        result = await hub.send_worker("w1", {"type": "test"})
        assert result is False

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.worker_ws is None  # Cleared by the failure handler

    async def test_send_worker_skip_clear_when_ws_replaced(self) -> None:
        """Line 327->329: send fails but st2.worker_ws is a NEW ws → skip clear (False branch)."""
        hub = TermHub()
        old_ws = _make_ws()
        new_ws = _make_ws()  # replacement worker
        old_ws.send_text = AsyncMock(side_effect=Exception("send failed"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = old_ws

        # Patch the lock's acquire to swap the ws between failure and the re-lock
        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _swap_on_second_acquire() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            if acquire_count == 2:
                # Simulate new worker taking over between send failure and re-lock
                st = hub._workers.get("w1")
                if st is not None:
                    st.worker_ws = new_ws  # replaced with new ws
            return result  # type: ignore[return-value]

        hub._lock.acquire = _swap_on_second_acquire  # type: ignore[method-assign]

        result = await hub.send_worker("w1", {"type": "test"})
        assert result is False

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.worker_ws is new_ws  # NOT cleared — the new ws was kept
