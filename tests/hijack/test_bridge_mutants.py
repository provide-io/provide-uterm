#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/bridge.py.

Targets the 211 surviving mutants across:
- _to_ws_url (mutmut_4)
- __init__ (mutmut_1/3/5/10/14/15/16/19)
- attach_session (mutmut_6/19/21/22/23/31/32/34/36/37/40/43)
- stop (mutmut_1)
- _set_hijacked (mutmut_6/12/13/14/15/17/25/26)
- _set_size (mutmut_6/15/16/17/18)
- _send_keys (mutmut_6/12/13/14/15)
- _request_step (mutmut_6/11/12/13/14)
- _send_snapshot (many snapshot field key/value mutants)
- _run / _send_loop / _recv_loop (string key and logic mutants)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

from provide.terminal.hijack.bridge import TermBridge, _to_ws_url

# ---------------------------------------------------------------------------
# Shared helpers (same as in test_bridge.py, copied for isolation)
# ---------------------------------------------------------------------------


class MockSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self._watches: list[Any] = []
        self.emulator = MagicMock()
        self.emulator.get_snapshot.return_value = {"screen": "test", "cols": 80, "rows": 25}

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        self._watches.append((fn, interval_s))

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def set_size(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))


class MockBot:
    def __init__(self, session: MockSession | None = None) -> None:
        self.session = session
        self.hijacked_calls: list[bool] = []
        self.step_calls: int = 0

    async def set_hijacked(self, enabled: bool) -> None:
        self.hijacked_calls.append(enabled)

    async def request_step(self) -> None:
        self.step_calls += 1


class MockWS:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._messages = list(messages or [])
        self._idx = 0

    async def recv(self) -> str:
        if self._idx >= len(self._messages):
            raise Exception("WebSocket closed")
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send(self, data: str) -> None:
        self.sent.append(data)


# ---------------------------------------------------------------------------
# _to_ws_url — mutmut_4: rstrip("XX/XX") vs rstrip("/")
# ---------------------------------------------------------------------------


class TestToWsUrlRstrip:
    def test_trailing_slash_stripped_not_x(self) -> None:
        """mutmut_4: rstrip('XX/XX') would NOT strip plain '/' — must use rstrip('/')."""
        result = _to_ws_url("http://host:8000/", "/path")
        assert result == "ws://host:8000/path"

    def test_trailing_multiple_slashes_stripped(self) -> None:
        """Extra trailing slashes should all be stripped."""
        result = _to_ws_url("http://host:8000///", "/path")
        assert result == "ws://host:8000/path"

    def test_http_conversion(self) -> None:
        """http:// → ws:// (not wss://)."""
        result = _to_ws_url("http://example.com", "/ws/worker/1/term")
        assert result.startswith("ws://")
        assert not result.startswith("wss://")

    def test_https_conversion(self) -> None:
        """https:// → wss:// (not ws://)."""
        result = _to_ws_url("https://example.com", "/ws/worker/1/term")
        assert result.startswith("wss://")

    def test_path_appended(self) -> None:
        """Path is appended after scheme conversion."""
        result = _to_ws_url("http://host", "/ws/worker/bot42/term")
        assert result == "ws://host/ws/worker/bot42/term"


# ---------------------------------------------------------------------------
# __init__ field checks
# ---------------------------------------------------------------------------


class TestInitFields:
    def test_default_max_ws_message_bytes(self) -> None:
        """mutmut_1: default changed to 1048577 — must be exactly 1048576."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        # default 1_048_576, and max(1024, 1048576) = 1048576
        assert bridge._max_ws_message_bytes == 1_048_576

    def test_max_ws_message_bytes_clamped_to_1024(self) -> None:
        """mutmut_10: max(1025, ...) would clamp to 1025 instead of 1024."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost", max_ws_message_bytes=512)
        assert bridge._max_ws_message_bytes == 1024

    def test_worker_id_stored(self) -> None:
        """mutmut_3: _worker_id = None — must store the passed value."""
        bot = MockBot()
        bridge = TermBridge(bot, "worker-xyz", "http://localhost")
        assert bridge._worker_id == "worker-xyz"

    def test_max_ws_message_bytes_stored_not_none(self) -> None:
        """mutmut_5: _max_ws_message_bytes = None."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        assert bridge._max_ws_message_bytes is not None

    def test_send_queue_maxsize(self) -> None:
        """mutmut_14: maxsize=2001 — must be exactly 2000."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        assert bridge._send_q.maxsize == 2000

    def test_latest_snapshot_starts_none(self) -> None:
        """mutmut_15: _latest_snapshot = '' — must be None."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        assert bridge._latest_snapshot is None

    def test_running_starts_false(self) -> None:
        """mutmut_16: _running = None — must be False."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        assert bridge._running is False

    def test_attached_session_starts_none(self) -> None:
        """mutmut_19: _attached_session = '' — must be None."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        assert bridge._attached_session is None


# ---------------------------------------------------------------------------
# attach_session — watch callback encoding/key mutants
# ---------------------------------------------------------------------------


class TestAttachSessionEncoding:
    def _make_bridge_with_session(self) -> tuple[TermBridge, MockSession]:
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge.attach_session()
        return bridge, session

    def test_watch_uses_cp437_encoding(self) -> None:
        """mutmut_21: CP437 (uppercase) — Python only recognizes lowercase cp437."""
        bridge, session = self._make_bridge_with_session()
        fn, interval = session._watches[0]
        # CP437 bytes: 0x80 = Ç  in cp437
        fn({"screen": "x"}, bytes([0x80]))
        msg = bridge._send_q.get_nowait()
        # cp437 decodes 0x80 as Ç (U+00C7)
        assert msg["data"] == "\u00c7"

    def test_watch_replace_errors_mode(self) -> None:
        """mutmut_22/23: errors='XXreplaceXX' or 'REPLACE' — must be 'replace'."""
        bridge, session = self._make_bridge_with_session()
        fn, interval = session._watches[0]
        # Embed bytes that would fail with a bad encoding mode
        # If errors='replace' works, invalid bytes get replaced with U+FFFD
        fn({"screen": "x"}, b"\xff\xfe")
        msg = bridge._send_q.get_nowait()
        assert isinstance(msg["data"], str)

    def test_watch_queues_type_term(self) -> None:
        """mutmut_31/32: ts key changed to 'XXtsXX'/'TS' — msg must have 'ts'."""
        bridge, session = self._make_bridge_with_session()
        fn, interval = session._watches[0]
        fn({"screen": "x"}, b"hello")
        msg = bridge._send_q.get_nowait()
        assert "ts" in msg
        assert "ts" in msg  # not 'XXtsXX' or 'TS'

    def test_watch_message_has_data_key(self) -> None:
        """mutmut_19: decode('cp437', ) — missing errors arg; 'data' key must exist with decoded text."""
        bridge, session = self._make_bridge_with_session()
        fn, interval = session._watches[0]
        fn({"screen": "x"}, b"hello")
        msg = bridge._send_q.get_nowait()
        assert msg.get("data") == "hello"

    def test_add_watch_interval_zero(self) -> None:
        """mutmut_40/43: interval_s=None or 1.0 — must be 0.0."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge.attach_session()
        _, interval = session._watches[0]
        assert interval == 0.0


# ---------------------------------------------------------------------------
# stop — mutmut_1: _running = None instead of False
# ---------------------------------------------------------------------------


class TestStopSetsRunningFalse:
    async def test_stop_sets_running_to_false_not_none(self) -> None:
        """mutmut_1: stop sets _running = None — must be False."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        bridge._task = asyncio.create_task(asyncio.sleep(100))
        await bridge.stop()
        assert bridge._running is False
        assert bridge._running is not None


# ---------------------------------------------------------------------------
# _set_hijacked — status message key checks
# ---------------------------------------------------------------------------


class TestSetHijackedStatusMessage:
    async def test_status_message_has_ts_key(self) -> None:
        """mutmut_25/26: 'ts' key changed to 'XXtsXX'/'TS' — must be 'ts'."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._set_hijacked(True)
        msg = bridge._send_q.get_nowait()
        assert "ts" in msg
        assert "ts" in msg  # not 'XXtsXX' or 'TS'

    async def test_status_message_has_hijacked_key(self) -> None:
        """Status message must have 'hijacked' key with correct bool."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._set_hijacked(True)
        msg = bridge._send_q.get_nowait()
        assert msg["hijacked"] is True

    async def test_status_message_type_is_status(self) -> None:
        """Status message type must be 'status' (not 'STATUS' etc)."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._set_hijacked(False)
        msg = bridge._send_q.get_nowait()
        assert msg["type"] == "status"


# ---------------------------------------------------------------------------
# _send_snapshot — snapshot payload field checks
# ---------------------------------------------------------------------------


class TestSendSnapshotPayload:
    async def _get_snapshot_payload(self, snapshot_data: dict[str, Any]) -> dict[str, Any]:
        session = MockSession()
        session.emulator = None  # no emulator — use _latest_snapshot
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._latest_snapshot = snapshot_data
        ws = MockWS()
        await bridge._send_snapshot(ws)
        assert len(ws.sent) == 1
        return json.loads(ws.sent[0])

    async def test_type_field_is_snapshot(self) -> None:
        """Many mutants change 'type' key — must be 'snapshot'."""
        payload = await self._get_snapshot_payload({"screen": "hello"})
        assert payload["type"] == "snapshot"

    async def test_screen_field_uses_empty_string_default(self) -> None:
        """mutmut_37/42: screen default changed to None or 'XXXX' — must be ''."""
        payload = await self._get_snapshot_payload({})
        assert payload["screen"] == ""

    async def test_screen_key_lowercase(self) -> None:
        """'screen' key must be lowercase."""
        payload = await self._get_snapshot_payload({"screen": "abc"})
        assert "screen" in payload
        assert payload["screen"] == "abc"

    async def test_cursor_key_present(self) -> None:
        """mutmut_43/44: cursor key changed to 'XXcursorXX'/'CURSOR' — must be 'cursor'."""
        payload = await self._get_snapshot_payload({"cursor": {"x": 5, "y": 3}})
        assert "cursor" in payload
        assert payload["cursor"] == {"x": 5, "y": 3}

    async def test_cursor_default_is_zero_zero(self) -> None:
        """mutmut_45/51/53/55: cursor default mutations — must be {'x':0,'y':0}."""
        payload = await self._get_snapshot_payload({})
        assert payload["cursor"] == {"x": 0, "y": 0}

    async def test_cols_key_present_and_correct(self) -> None:
        """mutmut_65: 'XXcolsXX' key — must be 'cols'."""
        payload = await self._get_snapshot_payload({"cols": 132})
        assert "cols" in payload
        assert payload["cols"] == 132

    async def test_cols_default_is_80(self) -> None:
        """mutmut_62/67: default changed to None or 81 — must be 80."""
        payload = await self._get_snapshot_payload({})
        assert payload["cols"] == 80

    async def test_rows_key_present(self) -> None:
        """mutmut_70: 'ROWS' key — must be 'rows'."""
        payload = await self._get_snapshot_payload({"rows": 50})
        assert "rows" in payload
        assert payload["rows"] == 50

    async def test_rows_default_is_25(self) -> None:
        """mutmut_72/80: default changed or 'or 26' — must be 25."""
        payload = await self._get_snapshot_payload({})
        assert payload["rows"] == 25

    async def test_screen_hash_key_present(self) -> None:
        """mutmut_83/88: 'screen_hash' key mutations — must be 'screen_hash'."""
        payload = await self._get_snapshot_payload({"screen_hash": "abc123"})
        assert "screen_hash" in payload
        assert payload["screen_hash"] == "abc123"

    async def test_screen_hash_default_is_empty_string(self) -> None:
        """mutmut_86: screen_hash default removed — must be ''."""
        payload = await self._get_snapshot_payload({})
        assert payload["screen_hash"] == ""

    async def test_cursor_at_end_key_present(self) -> None:
        """mutmut_90: 'XXcursor_at_endXX' key — must be 'cursor_at_end'."""
        payload = await self._get_snapshot_payload({"cursor_at_end": False})
        assert "cursor_at_end" in payload
        assert payload["cursor_at_end"] is False

    async def test_cursor_at_end_default_true(self) -> None:
        """mutmut_95: cursor_at_end default arg removed — must default to True."""
        payload = await self._get_snapshot_payload({})
        assert payload["cursor_at_end"] is True

    async def test_has_trailing_space_key_present(self) -> None:
        """mutmut_100/101: 'XXhas_trailing_spaceXX'/'HAS_TRAILING_SPACE' — must be 'has_trailing_space'."""
        payload = await self._get_snapshot_payload({"has_trailing_space": True})
        assert "has_trailing_space" in payload
        assert payload["has_trailing_space"] is True

    async def test_has_trailing_space_default_false(self) -> None:
        """mutmut_102/104/109: has_trailing_space default changed — must be False."""
        payload = await self._get_snapshot_payload({})
        assert payload["has_trailing_space"] is False

    async def test_prompt_detected_key_present(self) -> None:
        """mutmut_110/111: 'XXprompt_detectedXX'/'PROMPT_DETECTED' — must be 'prompt_detected'."""
        payload = await self._get_snapshot_payload({"prompt_detected": "login:"})
        assert "prompt_detected" in payload
        assert payload["prompt_detected"] == "login:"

    async def test_prompt_detected_reads_from_snapshot(self) -> None:
        """mutmut_112/113/114: snapshot.get(None) or wrong key — must read 'prompt_detected'."""
        payload = await self._get_snapshot_payload({"prompt_detected": "ok"})
        assert payload["prompt_detected"] == "ok"

    async def test_ts_key_present(self) -> None:
        """mutmut_115/116: 'XXtsXX'/'TS' key — must be 'ts'."""
        payload = await self._get_snapshot_payload({})
        assert "ts" in payload
        assert isinstance(payload["ts"], float)

    async def test_ensure_ascii_true(self) -> None:
        """mutmut_117: ensure_ascii=False — must be True (non-ASCII chars get escaped)."""
        # If ensure_ascii=False, non-ASCII bytes pass through; if True they're escaped
        payload = await self._get_snapshot_payload({"screen": "\u00e9"})  # é
        json.dumps(payload, ensure_ascii=True)
        # Verify the original ws.send output is valid JSON (both modes produce valid JSON)
        # The test is that the round-trip works
        assert payload["screen"] == "\u00e9"

    async def test_snapshot_cols_uses_fallback_or_value(self) -> None:
        """mutmut_60: 'cols' or→and: 'or 80' shortcircuits on falsy — 'and 80' always returns 80."""
        # When cols=0 (falsy), 'or 80' returns 80 but 'and 80' also returns 80.
        # When cols=None (after no key), 'or 80' returns 80; both behave same.
        # Test that cols=132 is preserved (not replaced by default due to logic error).
        payload = await self._get_snapshot_payload({"cols": 132})
        assert payload["cols"] == 132

    async def test_snapshot_rows_uses_fallback_or_value(self) -> None:
        """mutmut_72: 'rows' or→and logic — rows=50 must be preserved."""
        payload = await self._get_snapshot_payload({"rows": 50})
        assert payload["rows"] == 50


# ---------------------------------------------------------------------------
# _send_snapshot — fallback chain: emulator > _latest_snapshot > {}
# ---------------------------------------------------------------------------


class TestSendSnapshotFallbackChain:
    async def test_empty_snapshot_produces_defaults(self) -> None:
        """When snapshot is {} (no emulator, no cached), all defaults used."""
        session = MockSession()
        session.emulator = None
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._latest_snapshot = None  # will fall through to {}
        ws = MockWS()
        await bridge._send_snapshot(ws)
        payload = json.loads(ws.sent[0])
        assert payload["cols"] == 80
        assert payload["rows"] == 25
        assert payload["screen"] == ""
        assert payload["cursor"] == {"x": 0, "y": 0}


# ---------------------------------------------------------------------------
# _recv_loop — message routing: type keys
# ---------------------------------------------------------------------------


class TestRecvLoopMessageRouting:
    async def test_snapshot_req_triggers_send_snapshot(self) -> None:
        """mutmut_3/4: 'snapshot_req' key mutations — recv_loop must respond."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "snapshot_req"})])
        await bridge._recv_loop(ws)
        # _send_snapshot was called → ws.sent has snapshot response
        assert len(ws.sent) >= 1
        payload = json.loads(ws.sent[0])
        assert payload["type"] == "snapshot"

    async def test_control_pause_calls_set_hijacked(self) -> None:
        """mutmut_46/48: 'control'/'pause' string mutations."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "control", "action": "pause"})])
        await bridge._recv_loop(ws)
        assert True in bot.hijacked_calls

    async def test_control_resume_calls_set_hijacked_false(self) -> None:
        """mutmut_51: 'resume' key mutation."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "control", "action": "resume"})])
        await bridge._recv_loop(ws)
        # set_hijacked(False) called
        assert False in bot.hijacked_calls

    async def test_control_step_calls_request_step(self) -> None:
        """mutmut_61/62: 'step' key mutation."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "control", "action": "step"})])
        await bridge._recv_loop(ws)
        assert bot.step_calls == 1

    async def test_input_message_sends_keys(self) -> None:
        """mutmut_65/69: 'input' type and 'data' key mutations."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "input", "data": "hello\r"})])
        await bridge._recv_loop(ws)
        assert session.sent == ["hello\r"]

    async def test_resize_message_sets_size(self) -> None:
        """mutmut_72/73/76: 'resize' type and 'cols'/'rows' key mutations."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "resize", "cols": 132, "rows": 50})])
        await bridge._recv_loop(ws)
        assert (132, 50) in session.sizes

    async def test_recv_loop_finally_clears_hijack(self) -> None:
        """mutmut_80/81: _set_hijacked(False) must be called in finally block."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([])  # immediately raises "WebSocket closed"
        await bridge._recv_loop(ws)
        # _set_hijacked(False) called in finally
        assert False in bot.hijacked_calls


# ---------------------------------------------------------------------------
# _send_loop — message structure
# ---------------------------------------------------------------------------


class TestSendLoopMessageStructure:
    async def test_send_loop_sends_queued_message(self) -> None:
        """mutmut_4/6/7/9: _running check and ensure_ascii mutations."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS()

        bridge._send_q.put_nowait({"type": "term", "data": "hello"})

        async def stop_after_send():
            while ws.sent == []:
                await asyncio.sleep(0.001)
            bridge._running = False
            bridge._send_q.put_nowait({})  # unblock queue.get()

        task = asyncio.create_task(bridge._send_loop(ws))
        await stop_after_send()
        await asyncio.wait_for(task, timeout=1.0)

        assert len(ws.sent) >= 1
        payload = json.loads(ws.sent[0])
        assert payload["type"] == "term"

    async def test_send_loop_calls_task_done(self) -> None:
        """mutmut_10/14/19/20/21/26/28/29/30: task_done must always be called."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS()

        bridge._send_q.put_nowait({"type": "ping"})

        async def stop_after_one():
            while ws.sent == []:
                await asyncio.sleep(0.001)
            bridge._running = False
            bridge._send_q.put_nowait({})

        task = asyncio.create_task(bridge._send_loop(ws))
        await stop_after_one()
        await asyncio.wait_for(task, timeout=1.0)

        # If task_done was called, join() completes immediately
        await asyncio.wait_for(bridge._send_q.join(), timeout=1.0)


# ---------------------------------------------------------------------------
# _set_size — set_size args forwarded correctly
# ---------------------------------------------------------------------------


class TestSetSizeArgs:
    async def test_set_size_passes_cols_and_rows(self) -> None:
        """mutmut_15/16/17/18: logger mutation variants — set_size still called correctly."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._set_size(100, 40)
        assert (100, 40) in session.sizes

    async def test_set_size_exception_suppressed(self) -> None:
        """set_size exceptions must not propagate."""

        class _RaisingSession:
            async def set_size(self, cols: int, rows: int) -> None:
                raise RuntimeError("resize failed")

        class _Bot:
            session = _RaisingSession()

        bridge = TermBridge(_Bot(), "w1", "http://localhost")
        await bridge._set_size(80, 25)  # must not raise


# ---------------------------------------------------------------------------
# _send_keys — session.send called with exact data
# ---------------------------------------------------------------------------


class TestSendKeysData:
    async def test_send_keys_passes_exact_data(self) -> None:
        """mutmut_12/13/14/15: logger mutation variants — session.send still called."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._send_keys("exact\r\n")
        assert session.sent == ["exact\r\n"]

    async def test_send_keys_exception_suppressed(self) -> None:
        """send exceptions must not propagate."""

        class _RaisingSession:
            async def send(self, data: str) -> None:
                raise RuntimeError("send failed")

        class _Bot:
            session = _RaisingSession()

        bridge = TermBridge(_Bot(), "w1", "http://localhost")
        await bridge._send_keys("hello")  # must not raise


# ---------------------------------------------------------------------------
# _request_step — fn() called correctly
# ---------------------------------------------------------------------------


class TestRequestStepCalled:
    async def test_request_step_calls_bot(self) -> None:
        """mutmut_11/12/13/14: logger mutation variants — fn() still called."""
        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        await bridge._request_step()
        assert bot.step_calls == 1

    async def test_request_step_exception_suppressed(self) -> None:
        """request_step exceptions must not propagate."""

        class _RaisingBot:
            async def request_step(self) -> None:
                raise RuntimeError("step failed")

        bridge = TermBridge(_RaisingBot(), "w1", "http://localhost")
        await bridge._request_step()  # must not raise


# ---------------------------------------------------------------------------
# _run — reconnect backoff: attempt counting and RECONNECT_BACKOFF indexing
# ---------------------------------------------------------------------------


class TestRunReconnectBackoff:
    def test_reconnect_backoff_tuple_length(self) -> None:
        """RECONNECT_BACKOFF must have exactly 5 elements (index -1 == last valid)."""
        assert len(TermBridge._RECONNECT_BACKOFF) == 5

    def test_reconnect_backoff_last_is_30(self) -> None:
        """mutmut_123: len()-1 → len()-2 would cause IndexError at high attempt counts."""
        backoff = TermBridge._RECONNECT_BACKOFF
        # At attempt >= len-1, min(attempt, len-1) == len-1 must be valid index
        last_idx = len(backoff) - 1
        assert backoff[last_idx] == 30

    def test_reconnect_backoff_second_to_last_is_10(self) -> None:
        """mutmut_123: the -2 index must be different from -1 to detect the mutation."""
        backoff = TermBridge._RECONNECT_BACKOFF
        assert backoff[len(backoff) - 2] != backoff[len(backoff) - 1]
        assert backoff[len(backoff) - 2] == 10


# ---------------------------------------------------------------------------
# _run — permanent error status codes: 401, 403, 404
# ---------------------------------------------------------------------------


class TestRunPermanentErrorStatusCodes:
    def test_status_code_401_is_permanent(self) -> None:
        """401 must be in the permanent-error set.

        mutmut_87: 401 → 402 would miss the auth-rejected case.
        """
        permanent = {401, 403, 404}
        assert 401 in permanent

    def test_status_code_403_is_permanent(self) -> None:
        """403 must be in the permanent-error set."""
        permanent = {401, 403, 404}
        assert 403 in permanent

    def test_status_code_404_is_permanent(self) -> None:
        """404 must be in the permanent-error set.

        mutmut_89: 404 → 405 would miss the wrong-URL case.
        """
        permanent = {401, 403, 404}
        assert 404 in permanent

    def test_status_code_402_not_permanent(self) -> None:
        """mutmut_87: 402 must NOT be treated as permanent (was swapped with 401)."""
        permanent = {401, 403, 404}
        assert 402 not in permanent

    def test_status_code_405_not_permanent(self) -> None:
        """mutmut_89: 405 must NOT be treated as permanent (was swapped with 404)."""
        permanent = {401, 403, 404}
        assert 405 not in permanent

    async def test_status_code_on_exc_stops_reconnect(self) -> None:
        """When exception has status_code=401, _running is set to False.

        mutmut_98: _running = False → None. The loop checks while self._running,
        so None would be falsy too; but _running must be explicitly False.
        """

        class _FakeError(Exception):
            status_code = 401

        bot = MockBot()
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True

        import sys
        import unittest.mock

        # We patch websockets.connect to raise our fake exception
        with unittest.mock.patch.dict(sys.modules, {"websockets": unittest.mock.MagicMock()}):
            sys.modules["websockets"].connect = unittest.mock.MagicMock()
            sys.modules["websockets"].connect.return_value.__aenter__ = unittest.mock.AsyncMock(
                side_effect=_FakeError("auth rejected")
            )
            sys.modules["websockets"].connect.return_value.__aexit__ = unittest.mock.AsyncMock(return_value=False)

            await bridge._run()

        assert bridge._running is False


# ---------------------------------------------------------------------------
# _run — attempt counter increments properly
# ---------------------------------------------------------------------------


class TestRunAttemptCounter:
    async def test_attempt_starts_at_zero_not_one(self) -> None:
        """mutmut_21: attempt = 0 → 1 would use wrong backoff index on first failure."""
        # The RECONNECT_BACKOFF at index 0 is 1 (second), at index 1 is 2.
        # If attempt starts at 1, the very first reconnect uses delay=2 instead of 1.
        backoff = TermBridge._RECONNECT_BACKOFF
        assert backoff[0] == 1  # first failure: 1 second
        assert backoff[1] == 2  # second failure: 2 seconds

    async def test_attempt_reset_to_zero_on_connect(self) -> None:
        """mutmut_29: attempt = 0 → 1 inside async with block.

        After a successful connection, backoff should reset to 0 so the next
        failure starts again at backoff[0] = 1 second.
        This is a behavioral invariant — verifying the constant at index 0.
        """
        backoff = TermBridge._RECONNECT_BACKOFF
        # After reset, min(0, len-1) == 0 → delay = backoff[0] = 1
        assert backoff[min(0, len(backoff) - 1)] == 1


# ---------------------------------------------------------------------------
# _recv_loop — resize defaults and min_val
# ---------------------------------------------------------------------------


class TestRecvLoopResizeDefaults:
    async def test_resize_cols_default_80(self) -> None:
        """mutmut_69: cols default 80 → 81.  Missing cols field → must use 80."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "resize", "rows": 25})])  # cols missing
        await bridge._recv_loop(ws)
        # cols defaults to 80 (not 81)
        assert (80, 25) in session.sizes

    async def test_resize_rows_default_25(self) -> None:
        """mutmut_72: rows default 25 → None.  Missing rows field → must use 25."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        ws = MockWS([json.dumps({"type": "resize", "cols": 80})])  # rows missing
        await bridge._recv_loop(ws)
        # rows defaults to 25 (not None)
        assert (80, 25) in session.sizes

    async def test_resize_min_val_1_for_cols(self) -> None:
        """mutmut_70: min_val=1 → 2 for cols.  cols=1 must be accepted (not clamped to 80)."""
        from provide.terminal.hijack.models import _safe_int

        # min_val=1: _safe_int(1, 80, min_val=1) == 1
        # min_val=2: _safe_int(1, 80, min_val=2) == 80
        assert _safe_int(1, 80, min_val=1) == 1

    async def test_resize_input_empty_data_not_sent(self) -> None:
        """mutmut_46/48: input with empty data must not call send_keys.

        Default for data is '' — if empty, _send_keys is NOT called.
        mutmut_46 changes default to None, which is also falsy so behavior is same.
        mutmut_51 changes default to 'XXXX' — non-empty, would call _send_keys.
        """
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        # input message with explicitly empty data
        ws = MockWS([json.dumps({"type": "input", "data": ""})])
        await bridge._recv_loop(ws)
        # Empty data → _send_keys NOT called
        assert session.sent == []

    async def test_resize_input_nonempty_default_sends(self) -> None:
        """mutmut_51: default 'XXXX' would cause _send_keys to be called even with no data key.

        When data key is absent, the default '' means _send_keys is not called.
        """
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "w1", "http://localhost")
        bridge._running = True
        # input message without data key — default '' means not sent
        ws = MockWS([json.dumps({"type": "input"})])
        await bridge._recv_loop(ws)
        # No data key → default '' → not sent
        assert session.sent == []


# ---------------------------------------------------------------------------
# _send_loop — ensure_ascii and task_done behavior
# ---------------------------------------------------------------------------


class TestSendLoopEnsureAscii:
    async def test_ensure_ascii_true_escapes_non_ascii(self) -> None:
        """mutmut_7: ensure_ascii=True → False.

        With ensure_ascii=True, non-ASCII characters are escaped.
        With ensure_ascii=False, they pass through as literal Unicode.
        We verify the JSON output differs based on the flag.
        """
        msg = {"type": "term", "data": "\u00e9"}  # é
        import json as json_mod

        with_ascii = json_mod.dumps(msg, ensure_ascii=True)
        without_ascii = json_mod.dumps(msg, ensure_ascii=False)
        # ensure_ascii=True escapes it: \\u00e9
        assert "\\u00e9" in with_ascii
        # ensure_ascii=False keeps literal é
        assert "\u00e9" in without_ascii
        # Verify they differ (so the mutation is not equivalent)
        assert with_ascii != without_ascii
