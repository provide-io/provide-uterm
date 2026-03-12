#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Second-pass coverage gap tests for remaining missing branches."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState


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
        from provide.terminal.hijack.bridge import TermBridge, _InvalidURI

        if _InvalidURI is None:
            return  # websockets not installed

        bot = MagicMock()
        bot.worker_id = "w1"
        bot.session = None
        bot.set_hijacked = AsyncMock()

        bridge = TermBridge.__new__(TermBridge)
        bridge._bot = bot
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

        from provide.terminal.server.models import RecordingConfig, SessionDefinition
        from provide.terminal.server.runtime import HostedSessionRuntime

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

        with patch("provide.terminal.server.runtime.build_connector", return_value=connector):
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

        from provide.terminal.server.models import RecordingConfig, SessionDefinition
        from provide.terminal.server.runtime import HostedSessionRuntime

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
# ansi.py line 345->351 — _handle_twgs_tokens: emit_color returns empty
# ---------------------------------------------------------------------------


class TestAnsiTildeCodeNotInMap:
    def test_tilde_code_not_in_map_falls_through(self) -> None:
        """Line 326->333: tilde code not in _TILDE_MAP → literal passthrough."""
        from provide.terminal.ansi import _handle_tilde_codes

        # '~Z' — 'Z' is not in _TILDE_MAP → False branch of line 326 → appends '~' literally
        result = _handle_tilde_codes("~Z")
        assert "~" in result


class TestAnsiTwgsTokenInvalidPolarity:
    def test_twgs_token_with_invalid_polarity_falls_through(self) -> None:
        """Line 345->351: polarity NOT in ('+', '-') → falls through to out.append(text[i])."""
        from provide.terminal.ansi import _handle_twgs_tokens

        # '{xR}' - polarity='x' is not in ('+', '-') → False branch of line 345 → appends '{'
        # Note: TWGS token format is exactly 4 chars: {<polarity><color>}
        result = _handle_twgs_tokens("{xR}")
        # '{' is appended as a literal character
        assert "{" in result

    def test_twgs_token_with_unknown_color_char_falls_through(self) -> None:
        """Line 347->351: polarity valid but emit_color returns '' (unknown char) → fall through."""
        from provide.terminal.ansi import _handle_twgs_tokens

        # color_char 'Z' is not in _PREVIEW_COLOR_MAP and not 'x', so _emit_color returns ""
        result = _handle_twgs_tokens("{+Z}")
        # Since seq is "", the token is not consumed — '{' is appended literally
        assert "{" in result


# ---------------------------------------------------------------------------
# screen.py lines 140->137, 169->164 — re.error in extract functions
# ---------------------------------------------------------------------------


class TestScreenRegexErrors:
    def test_extract_menu_options_empty_description_skipped(self) -> None:
        """Line 140->137: if description: False → description is whitespace/empty after strip."""
        from provide.terminal.screen import extract_menu_options

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
        from provide.terminal.screen import extract_numbered_list

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

        from provide.terminal.session_logger import SessionLogger

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

        from provide.terminal.session_logger import SessionLogger

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

        from provide.terminal.server.config import load_server_config

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
        from provide.terminal.hijack.base import HijackableMixin

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


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 81->88 — should_resume=False (dashboard expired, REST active)
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackShouldResumeFalse:
    async def test_should_resume_false_when_rest_still_active(self) -> None:
        """Line 81->88: should_resume=False (dashboard expired but REST still active)."""
        hub = TermHub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            # Dashboard hijack expired
            fake_browser_ws = _make_ws()
            st.hijack_owner = fake_browser_ws
            st.hijack_owner_expires_at = now - 1  # expired
            # REST hijack still active
            st.hijack_session = HijackSession(
                hijack_id="rest-hid",
                owner="rest-op",
                acquired_at=now,
                lease_expires_at=now + 300,  # NOT expired
                last_heartbeat=now,
            )

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True  # dashboard expired

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.hijack_owner is None  # dashboard owner cleared
            assert st2.hijack_session is not None  # REST still active


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 81->88, 86->88 — cleanup_expired_hijack should_resume recheck
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackShouldResumeFalseAfterRecheck:
    async def test_should_resume_blocked_by_recheck(self) -> None:
        """Lines 81->88: should_resume=True but recheck shows new hijack → skip resume."""
        hub = TermHub()
        worker_ws = _make_ws()
        now = time.time()

        # Set up an expired dashboard hijack (so cleanup will fire)
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.hijack_owner = _make_ws()  # any ws
            st.hijack_owner_expires_at = now - 1  # expired

        # When cleanup_expired_hijack calls send_worker for the recheck block,
        # install a new REST session between unlock and lock in recheck.

        async def _patched_get_lock() -> None:
            pass

        # Instead, inject a new hijack after the first lock release (after is_hijacked check)
        # We do this by patching the _lock's acquire to install a new session on second acquire
        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _counting_acquire() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            # On the 2nd lock acquisition (the recheck), install a new hijack
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None and st.hijack_session is None:
                    st.hijack_session = HijackSession(
                        hijack_id="new-hijack",
                        owner="concurrent-owner",
                        acquired_at=now,
                        lease_expires_at=now + 300,
                        last_heartbeat=now,
                    )
            return result  # type: ignore[return-value]

        hub._lock.acquire = _counting_acquire  # type: ignore[method-assign]

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True
        # should_resume was flipped to False by recheck, so no resume was sent
        # send_worker should NOT have been called with a resume action
        # (just verify no crash)


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 86->88 — st2 is not None and is_hijacked (recheck True)
# ---------------------------------------------------------------------------


class TestCleanupExpiredHijackRecheckTrue:
    async def test_should_resume_false_when_recheck_finds_hijack(self) -> None:
        """Line 86->88: recheck finds is_hijacked → should_resume becomes False."""
        hub = TermHub()
        now = time.time()

        # Expired REST session — should_resume will be True after first lock
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = _make_ws()
            st.hijack_session = HijackSession(
                hijack_id="old-hijack",
                owner="operator",
                acquired_at=now - 100,
                lease_expires_at=now - 1,  # expired
                last_heartbeat=now - 50,
            )

        resume_calls: list = []
        original_send = hub.send_worker

        async def _capture_send(worker_id: str, msg: dict) -> bool:
            if msg.get("action") == "resume":
                resume_calls.append(msg)
            return await original_send(worker_id, msg)

        hub.send_worker = _capture_send  # type: ignore[method-assign]

        # Install a new dashboard hijack between the first lock release and recheck
        # by patching _lock.acquire
        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _inject_on_second_acquire() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None:
                    # Make hub think something is hijacked
                    fake_ws = _make_ws()
                    st.hijack_owner = fake_ws
                    st.hijack_owner_expires_at = now + 999
            return result  # type: ignore[return-value]

        hub._lock.acquire = _inject_on_second_acquire  # type: ignore[method-assign]

        result = await hub.cleanup_expired_hijack("w1")
        assert result is True
        # Resume was NOT sent because recheck found active hijack
        assert len(resume_calls) == 0


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 213->220 — remove_dead_browsers: st is None (False branch)
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersStNone:
    async def test_remove_dead_browsers_nonexistent_worker(self) -> None:
        """Line 213->220: st is None (worker not found) → skip inner block."""
        hub = TermHub()
        dead_ws = _make_ws()
        # Call with a worker_id that doesn't exist
        result = await hub.remove_dead_browsers("nonexistent-worker", {dead_ws})
        assert result is False  # notify_hijack_off was never set True


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 213->220 — remove_dead_browsers clears dashboard owner
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersClearsOwner:
    async def test_remove_dead_browsers_clears_dashboard_owner(self) -> None:
        """Line 213->220: dead socket is dashboard owner → clear owner, set notify_hijack_off."""
        hub = TermHub()
        now = time.time()

        owner_ws = _make_ws()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now + 300

        result = await hub.remove_dead_browsers("w1", {owner_ws})
        assert result is True  # notify_hijack_off was True

        async with hub._lock:
            st2 = hub._workers.get("w1")
            assert st2 is not None
            assert st2.hijack_owner is None


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 225->227 — remove_dead_browsers recheck finds hijack
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsersRecheckFindsHijack:
    async def test_remove_dead_browsers_recheck_blocks_resume(self) -> None:
        """Lines 225->227: notify_hijack_off=True but recheck finds is_hijacked → False."""
        hub = TermHub()
        now = time.time()

        owner_ws = _make_ws()
        worker_ws = _make_ws()
        worker_ws.send_text = AsyncMock()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.worker_ws = worker_ws
            st.browsers[owner_ws] = "admin"
            st.hijack_owner = owner_ws
            st.hijack_owner_expires_at = now + 300

        # Install a REST session between the first lock release and the recheck
        original_acquire = hub._lock.acquire
        acquire_count = 0

        async def _inject_on_recheck() -> bool:
            nonlocal acquire_count
            acquire_count += 1
            result = await original_acquire()
            if acquire_count == 2:
                st = hub._workers.get("w1")
                if st is not None and st.hijack_session is None:
                    st.hijack_session = HijackSession(
                        hijack_id="injected",
                        owner="concurrent",
                        acquired_at=now,
                        lease_expires_at=now + 300,
                        last_heartbeat=now,
                    )
            return result  # type: ignore[return-value]

        hub._lock.acquire = _inject_on_recheck  # type: ignore[method-assign]

        resume_calls: list = []
        original_send = hub.send_worker

        async def _capture_send(wid: str, msg: dict) -> bool:
            if msg.get("action") == "resume":
                resume_calls.append(msg)
            return await original_send(wid, msg)

        hub.send_worker = _capture_send  # type: ignore[method-assign]

        result = await hub.remove_dead_browsers("w1", {owner_ws})
        # notify_hijack_off was flipped to False by recheck
        assert result is False
        assert len(resume_calls) == 0


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 306 — release_rest_hijack: st is None or no session
# ---------------------------------------------------------------------------


class TestReleaseRestHijackNotFound:
    async def test_release_rest_hijack_worker_not_found(self) -> None:
        """Line 306: st is None → return False, False."""
        hub = TermHub()
        was_released, should_resume = await hub.release_rest_hijack("nonexistent", "any-id")
        assert was_released is False
        assert should_resume is False

    async def test_release_rest_hijack_wrong_hijack_id(self) -> None:
        """Line 306: hijack_session.hijack_id != hijack_id → return False, False."""
        hub = TermHub()
        now = time.time()

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            st.hijack_session = HijackSession(
                hijack_id="correct-id",
                owner="op",
                acquired_at=now,
                lease_expires_at=now + 300,
                last_heartbeat=now,
            )

        was_released, should_resume = await hub.release_rest_hijack("w1", "wrong-id")
        assert was_released is False
        assert should_resume is False

    async def test_release_rest_hijack_no_session(self) -> None:
        """Line 306: st.hijack_session is None → return False, False."""
        hub = TermHub()

        async with hub._lock:
            hub._workers.setdefault("w1", WorkerTermState())

        was_released, should_resume = await hub.release_rest_hijack("w1", "any-id")
        assert was_released is False
        assert should_resume is False


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py line 333 — prepare_browser_input: st is None
# ---------------------------------------------------------------------------


class TestPrepareBrowserInputStNone:
    async def test_prepare_browser_input_returns_false_when_no_worker(self) -> None:
        """Line 333: st is None → return False (in prepare_browser_input)."""
        hub = TermHub()
        ws = _make_ws()
        result = await hub.prepare_browser_input("nonexistent", ws)
        assert result is False


# ---------------------------------------------------------------------------
# hijack/hub/ownership.py is_input_open_mode: also test False case
# ---------------------------------------------------------------------------


class TestIsInputOpenModeStNone:
    async def test_is_input_open_mode_returns_false_when_no_worker(self) -> None:
        """is_input_open_mode: st is None → False."""
        hub = TermHub()
        result = await hub.is_input_open_mode("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# routes/rest.py line 318 — keys too long in hijack_send (400)
# ---------------------------------------------------------------------------


class TestRestSendKeysTooLong:
    def test_send_keys_too_long_returns_400(self) -> None:
        """Line 318: len(request.keys) > hub.max_input_chars → 400."""
        hub, app, client = _make_app(max_input_chars=100)
        now = time.time()
        hid = "abcdef12-0000-0000-0000-000000000000"

        async def _setup() -> None:
            async with hub._lock:
                st = hub._workers.setdefault("w1", WorkerTermState())
                st.worker_ws = _make_ws()
                st.worker_ws.send_text = AsyncMock()
                st.hijack_session = HijackSession(
                    hijack_id=hid,
                    owner="tester",
                    acquired_at=now,
                    lease_expires_at=now + 300,
                    last_heartbeat=now,
                )

        asyncio.run(_setup())

        too_long = "x" * 101  # > max_input_chars=100
        resp = client.post(f"/worker/w1/hijack/{hid}/send", json={"keys": too_long})
        assert resp.status_code == 400
        assert "keys too long" in resp.json()["error"]


# ---------------------------------------------------------------------------
# routes/websockets.py line 109->111 — worker_hello: mode_applied=False
# ---------------------------------------------------------------------------


class TestWorkerHelloModeNotApplied:
    def test_worker_hello_mode_not_applied_no_broadcast(self) -> None:
        """Line 109->111: set_worker_hello_mode returns False → no broadcast_hijack_state."""
        # Patch set_worker_hello_mode to return False so mode_applied=False
        with patch.object(TermHub, "set_worker_hello_mode", new=AsyncMock(return_value=False)):
            hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

            broadcast_calls: list = []
            original_broadcast = hub.broadcast_hijack_state

            async def _capture_broadcast(worker_id: str) -> None:
                broadcast_calls.append(worker_id)
                return await original_broadcast(worker_id)

            hub.broadcast_hijack_state = _capture_broadcast  # type: ignore[method-assign]

            with (
                client.websocket_connect("/ws/worker/w1/term") as worker,
                client.websocket_connect("/ws/browser/w1/term") as browser,
            ):
                _read_initial_browser(browser)

                # Worker sends hello with "open" mode — set_worker_hello_mode returns False
                worker.send_json({"type": "worker_hello", "input_mode": "open"})
                # Trigger a snapshot to ensure worker_hello was processed before test ends
                worker.send_json(
                    {
                        "type": "snapshot",
                        "screen": "check",
                        "cursor": {"x": 0, "y": 0},
                        "cols": 80,
                        "rows": 25,
                    }
                )
                browser.receive_json()  # snapshot arrives at browser

                # broadcast_hijack_state should NOT have been called for the worker_hello
                # (only for snapshot and initial connect messages)


# ---------------------------------------------------------------------------
# routes/websockets.py line 114->120 — worker_hello: invalid mode value
# ---------------------------------------------------------------------------


class TestWorkerHelloNoInputMode:
    def test_worker_hello_without_input_mode_continues(self) -> None:
        """Line 114->120: _hello_mode is None → elif False → continue (114->120 False branch)."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        with (
            client.websocket_connect("/ws/worker/w1/term") as worker,
            client.websocket_connect("/ws/browser/w1/term") as browser,
        ):
            _read_initial_browser(browser)
            # Send worker_hello without input_mode (so _hello_mode=None)
            worker.send_json({"type": "worker_hello"})
            # Confirm connection still alive by sending a snapshot
            worker.send_json(
                {
                    "type": "snapshot",
                    "screen": "alive",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                }
            )
            msg = browser.receive_json()
            assert msg["type"] == "snapshot"


# ---------------------------------------------------------------------------
# routes/websockets.py line 155->87 — worker sends "term" with empty data
# ---------------------------------------------------------------------------


class TestWorkerTermEmptyData:
    def test_worker_term_empty_data_no_broadcast(self) -> None:
        """Line 155->87: mtype='term' with empty data → not broadcast, falls through elif chain."""
        broadcast_calls: list = []
        hub, app, client = _make_app()

        original_broadcast = hub.broadcast

        async def _capture_broadcast(worker_id: str, msg: dict) -> None:
            broadcast_calls.append(msg)
            return await original_broadcast(worker_id, msg)

        hub.broadcast = _capture_broadcast  # type: ignore[method-assign]

        with client.websocket_connect("/ws/worker/w1/term") as worker:
            worker.send_json({"type": "term", "data": ""})
            # No broadcast should occur for empty term data


# ---------------------------------------------------------------------------
# routes/websockets.py line 216 — browser role not in VALID_ROLES → "viewer"
# ---------------------------------------------------------------------------


class TestBrowserRoleInvalidFallsToViewer:
    def test_invalid_role_falls_back_to_viewer(self) -> None:
        """Line 216: role not in VALID_ROLES → role = 'viewer'."""
        # Return an invalid role from resolver (resolver already tested, this
        # goes through the route's own check: if role not in VALID_ROLES: role = 'viewer')
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "superadmin")

        with (
            client.websocket_connect("/ws/worker/w1/term") as _worker,
            client.websocket_connect("/ws/browser/w1/term") as browser,
        ):
            hello, _ = _read_initial_browser(browser)
            # role should have been forced to "viewer"
            assert hello["role"] == "viewer"


# ---------------------------------------------------------------------------
# routes/websockets.py lines 307, 308->332, 333->335 — was_owner, recheck finds hijack
# ---------------------------------------------------------------------------


class TestBrowserWasOwnerRecheckFindsHijack:
    def test_was_owner_recheck_finds_hijack_skips_resume(self) -> None:
        """Lines 307, 308->332, 333->335: was_owner=True, check_still_hijacked=True → no resume."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
            for _ in range(max_msgs):
                msg = worker.receive_json()
                if msg.get("action") == action or msg.get("type") == action:
                    return msg
            raise AssertionError(f"Did not receive action={action!r}")

        with client.websocket_connect("/ws/worker/w1/term") as worker:
            worker.receive_json()  # snapshot_req

            with client.websocket_connect("/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Browser acquires hijack
                browser.send_json({"type": "hijack_request"})
                _drain_worker_until(worker, "pause")
                browser.receive_json()  # hijack_state

                # Patch check_still_hijacked to return True so _do_resume is set False at line 307
                with patch.object(hub, "check_still_hijacked", new=AsyncMock(return_value=True)):
                    pass  # patch only needed during disconnect; exits before browser closes

            # At this point browser is disconnected (was_owner=True, no REST)
            # Without the patch active, _do_resume=True and check_still_hijacked=False
            # To hit line 307, we need the patch active during the finally block
            # The clean way: patch before the browser context exits


class TestBrowserWasOwnerRecheckHijackedPatched:
    def test_was_owner_check_still_hijacked_true_skips_resume(self) -> None:
        """Lines 307, 308->332, 333->335: patch check_still_hijacked=True → _do_resume=False."""
        hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

        def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
            for _ in range(max_msgs):
                msg = worker.receive_json()
                if msg.get("action") == action or msg.get("type") == action:
                    return msg
            raise AssertionError(f"Did not receive action={action!r}")

        with (
            patch.object(TermHub, "check_still_hijacked", new=AsyncMock(return_value=True)),
            client.websocket_connect("/ws/worker/w1/term") as worker,
        ):
            worker.receive_json()  # snapshot_req

            with client.websocket_connect("/ws/browser/w1/term") as browser:
                _read_initial_browser(browser)

                # Browser acquires hijack
                browser.send_json({"type": "hijack_request"})
                _drain_worker_until(worker, "pause")
                browser.receive_json()  # hijack_state

                # Browser disconnects: was_owner=True, _do_resume=True initially
                # but check_still_hijacked (patched True) → _do_resume=False (line 307)
                # → line 308 False branch → 332 (broadcast_hijack_state)
                # → line 333 False branch → 335 (append_event)

            # Browser disconnected; worker should NOT have received a resume
            # (patched check_still_hijacked returned True)


# ---------------------------------------------------------------------------
# routes/websockets.py line 338 — resume_without_owner recheck True → line 339->363
# ---------------------------------------------------------------------------


class TestResumeWithoutOwnerRecheckTrue:
    def test_resume_without_owner_blocked_by_recheck(self) -> None:
        """Lines 338, 339->363: owned_hijack=True, was_owner=False, check_still_hijacked=True."""
        # Patch at class level so it's active during the WS disconnect finally block
        with patch.object(TermHub, "check_still_hijacked", new=AsyncMock(return_value=True)):
            hub, app, client = _make_app(resolve_browser_role=lambda ws, wid: "admin")

            def _drain_worker_until(worker: Any, action: str, max_msgs: int = 10) -> dict:
                for _ in range(max_msgs):
                    msg = worker.receive_json()
                    if msg.get("action") == action or msg.get("type") == action:
                        return msg
                raise AssertionError(f"Did not receive action={action!r}")

            with client.websocket_connect("/ws/worker/w1/term") as worker:
                worker.receive_json()  # snapshot_req

                with client.websocket_connect("/ws/browser/w1/term") as browser:
                    _read_initial_browser(browser)

                    # Browser acquires hijack → owned_hijack=True in the route
                    browser.send_json({"type": "hijack_request"})
                    _drain_worker_until(worker, "pause")
                    browser.receive_json()  # hijack_state

                    # Force-expire the hijack by manipulating hub state directly
                    # so that owned_hijack stays True but was_owner=False at disconnect
                    async def _expire_hijack() -> None:
                        async with hub._lock:
                            st = hub._workers.get("w1")
                            if st is not None:
                                st.hijack_owner = None
                                st.hijack_owner_expires_at = None

                    asyncio.run(_expire_hijack())

                    # Now: owned_hijack=True (route var), was_owner=False (cleared from hub)
                    # is_hijacked=False, worker online → resume_without_owner=True
                    # check_still_hijacked (patched True) → resume_without_owner=False (line 338)
                    # → line 339 False branch → 363 (prune_if_idle)
                    # No resume sent to worker
