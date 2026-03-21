#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/runtime.py — BridgeSession state machine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.server.models import AuthConfig, RecordingConfig, SessionDefinition
from provide.terminal.server.registry import SessionRegistry
from provide.terminal.server.runtime import HostedSessionRuntime

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_session(
    session_id: str = "test-session",
    connector_type: str = "shell",
    auto_start: bool = False,
    ephemeral: bool = False,
    owner: str | None = None,
    input_mode: str = "open",
    visibility: str = "public",
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test Session",
        connector_type=connector_type,
        auto_start=auto_start,
        ephemeral=ephemeral,
        owner=owner,
        input_mode=input_mode,  # type: ignore[arg-type]
        visibility=visibility,  # type: ignore[arg-type]
    )


def _make_runtime(
    session_id: str = "test-session",
    base_url: str = "http://localhost:9999",
    recording: RecordingConfig | None = None,
    worker_bearer_token: str | None = None,
) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(session_id),
        public_base_url=base_url,
        recording=recording or RecordingConfig(),
        worker_bearer_token=worker_bearer_token,
    )


def _make_hub() -> MagicMock:
    hub = MagicMock()
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.get_last_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[])
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    return hub


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    hub: MagicMock | None = None,
    recording: RecordingConfig | None = None,
    max_sessions: int | None = None,
) -> SessionRegistry:
    h = hub or _make_hub()
    return SessionRegistry(
        sessions or [],
        hub=h,
        public_base_url="http://localhost:9999",
        recording=recording or RecordingConfig(),
        max_sessions=max_sessions,
    )


def _jwt_auth_config(key: str = _TEST_KEY) -> AuthConfig:
    import jwt as pyjwt

    now = int(time.time())
    worker_token = pyjwt.encode(
        {"sub": "worker", "exp": now + 600, "iss": "provide-terminal", "aud": "provide-terminal-server"},
        key=key,
        algorithm="HS256",
    )
    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-terminal",
        jwt_audience="provide-terminal-server",
        worker_bearer_token=worker_token,
    )


def _make_jwt_token(
    sub: str = "user1",
    roles: Any = None,
    exp_offset: int = 600,
    key: str = _TEST_KEY,
) -> str:
    import jwt as pyjwt

    if roles is None:
        roles = ["operator"]
    now = int(time.time())
    payload = {
        "sub": sub,
        "roles": roles,
        "iss": "provide-terminal",
        "aud": "provide-terminal-server",
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    return pyjwt.encode(payload, key=key, algorithm="HS256")


# ---------------------------------------------------------------------------
# Bridge session helpers
# ---------------------------------------------------------------------------


class _FakeWs:
    """Minimal fake websocket for bridge_session testing."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._sent: list[str] = []
        self._index = 0

    async def send(self, data: str) -> None:
        self._sent.append(data)

    async def recv(self) -> str:
        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1
            return msg
        # block until stop
        await asyncio.sleep(10)
        raise RuntimeError("no more messages")


class _FakeConnector:
    def __init__(self, poll_results: list[list[dict[str, Any]]] | None = None) -> None:
        self._poll_iter = iter(poll_results or [])
        self.set_mode_result: list[dict[str, Any]] = []
        self.snapshot_result: dict[str, Any] = {"type": "snapshot", "screen": "initial"}
        self.handle_input_result: list[dict[str, Any]] = []
        self.handle_control_result: list[dict[str, Any]] = []
        self.get_analysis_result: str = "analysis text"
        self.input_received: list[str] = []
        self.control_received: list[str] = []

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        return self.set_mode_result

    async def get_snapshot(self) -> dict[str, Any]:
        return self.snapshot_result

    async def poll_messages(self) -> list[dict[str, Any]]:
        try:
            return next(self._poll_iter)
        except StopIteration:
            await asyncio.sleep(0.05)
            return []

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        self.input_received.append(data)
        return self.handle_input_result

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        self.control_received.append(action)
        return self.handle_control_result

    async def get_analysis(self) -> str:
        return self.get_analysis_result


async def _run_bridge_until_stop(
    rt: HostedSessionRuntime, ws: _FakeWs, connector: _FakeConnector | None = None
) -> None:
    """Run _bridge_session for a short time then set stop."""
    if connector is None:
        connector = _FakeConnector()
    rt._connector = connector
    rt._stop.clear()
    asyncio.create_task(rt._bridge_session(ws))  # noqa: RUF006
    await asyncio.sleep(0.15)
    rt._stop.set()


# ===========================================================================
# runtime.py — HostedSessionRuntime.__init__
# ===========================================================================


class TestBridgeSession:
    async def test_bridge_raises_when_no_connector(self) -> None:
        """mutmut_4: error message changed — but type must still be RuntimeError."""
        rt = _make_runtime()
        rt._connector = None
        ws = _FakeWs([])
        with pytest.raises(RuntimeError):
            await rt._bridge_session(ws)

    async def test_bridge_sets_state_running(self) -> None:
        rt = _make_runtime()
        connector = _FakeConnector()
        rt._connector = connector
        rt._queue = asyncio.Queue()
        rt._stop.clear()

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(_FakeWs([]))
        assert rt._state == "running"

    async def test_log_event_called_with_runtime_started(self) -> None:
        """mutmut_14/15/18/19/21: _log_event called with wrong args."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        events: list[tuple[str, dict[str, Any]]] = []

        orig_log = rt._log_event

        async def _capture_log_event(event: str, payload: dict[str, Any]) -> None:
            events.append((event, payload))
            await orig_log(event, payload)

        rt._log_event = _capture_log_event  # type: ignore[method-assign]
        connector = _FakeConnector()
        rt._connector = connector

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(_FakeWs([]))

        assert any(event == "runtime_started" for event, _ in events)
        # The payload must have "session_id" key (not SESSION_ID)
        for event, payload in events:
            if event == "runtime_started":
                assert "session_id" in payload

    async def test_snapshot_from_queue_calls_log_snapshot(self) -> None:
        """mutmut_29/30/31/32/33/34: snapshot type check broken."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()

        logged: list[dict[str, Any]] = []

        async def _log_snap(msg: dict[str, Any]) -> None:
            logged.append(msg)

        rt._log_snapshot = _log_snap  # type: ignore[method-assign]

        snap_msg = {"type": "snapshot", "screen": "test"}
        await rt._queue.put(snap_msg)

        connector = _FakeConnector()
        rt._connector = connector
        ws = _FakeWs([])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        assert snap_msg in logged

    async def test_non_snapshot_from_queue_not_logged(self) -> None:
        """Confirm type check is case-sensitive — only 'snapshot' triggers logging."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()

        logged: list[dict[str, Any]] = []

        async def _log_snap(msg: dict[str, Any]) -> None:
            logged.append(msg)

        rt._log_snapshot = _log_snap  # type: ignore[method-assign]

        non_snap = {"type": "other", "screen": "x"}
        await rt._queue.put(non_snap)

        connector = _FakeConnector()
        rt._connector = connector
        ws = _FakeWs([])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        assert non_snap not in logged

    async def test_input_message_dispatched_to_connector(self) -> None:
        """mutmut_116/118/121: data default changed."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        rt._connector = connector
        ws = _FakeWs([json.dumps({"type": "input", "data": "hello"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        assert "hello" in connector.input_received

    async def test_input_missing_data_uses_empty_string(self) -> None:
        """mutmut_116: default=None causes str(None)='None' not ''."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        rt._connector = connector
        # No "data" key in message
        ws = _FakeWs([json.dumps({"type": "input"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        # Must receive empty string, not "None"
        assert connector.input_received == [""]

    async def test_analyze_req_returns_analysis_type(self) -> None:
        """mutmut_91/92: type key changed to XXanalysisXX or ANALYSIS."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        connector.get_analysis_result = "AI says: hello"
        rt._connector = connector

        sent_messages: list[dict[str, Any]] = []

        class _CapturingWs(_FakeWs):
            async def send(self, data: str) -> None:
                sent_messages.append(json.loads(data))

        ws = _CapturingWs([json.dumps({"type": "analyze_req"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        analysis_msgs = [m for m in sent_messages if m.get("type") == "analysis"]
        assert analysis_msgs, "Expected an 'analysis' response message"
        assert analysis_msgs[0]["formatted"] == "AI says: hello"

    async def test_analyze_req_response_has_formatted_key(self) -> None:
        """mutmut_93/94: formatted key renamed."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        connector.get_analysis_result = "result"
        rt._connector = connector

        sent_messages: list[dict[str, Any]] = []

        class _CapWs(_FakeWs):
            async def send(self, data: str) -> None:
                sent_messages.append(json.loads(data))

        ws = _CapWs([json.dumps({"type": "analyze_req"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        for m in sent_messages:
            if m.get("type") == "analysis":
                assert "formatted" in m, "'formatted' key missing from analysis response"

    async def test_analyze_req_response_has_ts_key(self) -> None:
        """mutmut_95/96: ts key renamed."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        rt._connector = connector

        sent_messages: list[dict[str, Any]] = []

        class _CapWs(_FakeWs):
            async def send(self, data: str) -> None:
                sent_messages.append(json.loads(data))

        ws = _CapWs([json.dumps({"type": "analyze_req"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        for m in sent_messages:
            if m.get("type") == "analysis":
                assert "ts" in m, "'ts' key missing from analysis response"

    async def test_control_message_uses_action_default_empty_string(self) -> None:
        """mutmut_104/106/109: action default changed."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        rt._connector = connector
        # Message with no "action" key
        ws = _FakeWs([json.dumps({"type": "control"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        # handle_control must be called with "" (empty string), not "None" or "XXXX"
        assert connector.control_received == [""]

    async def test_invalid_json_continues_not_breaks(self) -> None:
        """mutmut_75: continue replaced with break — loop would exit on bad JSON."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()
        connector = _FakeConnector()
        rt._connector = connector

        # First message: invalid JSON; second: valid input
        ws = _FakeWs(["NOT_JSON", json.dumps({"type": "input", "data": "ok"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.15)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        # Should have processed the valid "input" message despite invalid JSON first
        assert "ok" in connector.input_received

    async def test_poll_snapshot_triggers_log_snapshot(self) -> None:
        """mutmut_62/63/64/66/67/68: type check on poll results broken."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()

        snap_msg = {"type": "snapshot", "screen": "polled"}
        connector = _FakeConnector(poll_results=[[snap_msg], []])
        rt._connector = connector

        logged: list[dict[str, Any]] = []

        async def _log_snap(msg: dict[str, Any]) -> None:
            logged.append(msg)

        rt._log_snapshot = _log_snap  # type: ignore[method-assign]
        ws = _FakeWs([])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.2)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        assert snap_msg in logged

    async def test_response_snapshot_triggers_log_snapshot(self) -> None:
        """mutmut_127/128/129/130/131/132/133: type check on response outbound broken."""
        rt = _make_runtime()
        rt._queue = asyncio.Queue()
        rt._stop.clear()

        snap_response = {"type": "snapshot", "screen": "response-snap"}

        connector = _FakeConnector()
        connector.snapshot_result = snap_response
        rt._connector = connector

        logged: list[dict[str, Any]] = []

        async def _log_snap(msg: dict[str, Any]) -> None:
            logged.append(msg)

        rt._log_snapshot = _log_snap  # type: ignore[method-assign]
        ws = _FakeWs([json.dumps({"type": "snapshot_req"})])

        async def _stop_soon() -> None:
            await asyncio.sleep(0.15)
            rt._stop.set()

        asyncio.create_task(_stop_soon())  # noqa: RUF006
        with contextlib.suppress(Exception):
            await rt._bridge_session(ws)

        assert snap_response in logged


# ===========================================================================
# runtime.py — HostedSessionRuntime._run() backoff and error handling
# ===========================================================================
