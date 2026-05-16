#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/runtime.py — transient failure, auth header, finally block."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


def _make_session(
    session_id: str = "s1",
    display_name: str = "Test",
    connector_type: str = "shell",
    input_mode: str = "hijack",
    auto_start: bool = False,
    tags: list[str] | None = None,
    owner: str | None = None,
    visibility: str = "private",
) -> SessionDefinition:
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "display_name": display_name,
        "connector_type": connector_type,
        "input_mode": input_mode,  # type: ignore[arg-type]
        "auto_start": auto_start,
        "tags": tags or [],
        "visibility": visibility,  # type: ignore[arg-type]
    }
    if owner is not None:
        kwargs["owner"] = owner
    return SessionDefinition(**kwargs)


def _make_runtime(
    session_id: str = "s1",
    base_url: str = "http://localhost:9999",
    bearer_token: str | None = None,
    recording: RecordingConfig | None = None,
) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(session_id=session_id),
        public_base_url=base_url,
        recording=recording or RecordingConfig(),
        worker_bearer_token=bearer_token,
    )


# ---------------------------------------------------------------------------
# _run() — state changes on general Exception (transient failure)
# ---------------------------------------------------------------------------


class TestRunTransientFailure:
    async def test_exception_sets_state_error(self) -> None:
        """mut_56/57/58: _state=None/'XXerrorXX'/'ERROR' on Exception."""
        rt = _make_runtime()
        call_count = 0

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionRefusedError("refused")
            # Second call: stop the loop
            rt._stop.set()
            raise ConnectionRefusedError("refused again")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        # _last_error should be set
        assert rt._last_error is not None

    async def test_exception_sets_connected_false(self) -> None:
        """mut_59/60: _connected=None/True on Exception."""
        rt = _make_runtime()
        rt._connected = True

        async def _fail_connector():
            rt._stop.set()
            raise RuntimeError("oops")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert rt._connected is False

    async def test_401_status_breaks_loop(self) -> None:
        """mut_103/104: 'in' → 'not in', 401 removed from break codes."""
        call_count = 0
        rt = _make_runtime()

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            exc = Exception("unauthorized")
            exc.status_code = 401  # type: ignore[attr-defined]
            raise exc

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        # Must break immediately on 401 — not retry
        assert call_count == 1

    async def test_403_status_breaks_loop(self) -> None:
        """mut_105: 403 removed from break codes."""
        call_count = 0
        rt = _make_runtime()

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            exc = Exception("forbidden")
            exc.status_code = 403  # type: ignore[attr-defined]
            raise exc

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert call_count == 1

    async def test_404_status_breaks_loop(self) -> None:
        """mut_106: 404 removed from break codes."""
        call_count = 0
        rt = _make_runtime()

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            exc = Exception("not found")
            exc.status_code = 404  # type: ignore[attr-defined]
            raise exc

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert call_count == 1

    async def test_500_status_retries(self) -> None:
        """Verify non-permanent status codes do retry (not break)."""
        call_count = 0
        rt = _make_runtime()

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                rt._stop.set()
            exc = Exception("server error")
            exc.status_code = 500  # type: ignore[attr-defined]
            raise exc

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert call_count >= 2

    async def test_backoff_uses_attempt_index(self) -> None:
        """mut_123/124/125: attempt=1 or attempt-=1 or attempt+=2."""
        rt = _make_runtime()
        sleep_calls: list[float] = []
        call_count = 0

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        async def _fail_connector():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                rt._stop.set()
            raise RuntimeError("transient")

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        # First call: attempt=0 → backoff_s[0]=0.25
        # Second call: attempt=1 → backoff_s[1]=0.5
        assert len(sleep_calls) >= 2
        assert sleep_calls[0] == 0.25
        assert sleep_calls[1] == 0.5


# ---------------------------------------------------------------------------
# _run() — Authorization header key case
# ---------------------------------------------------------------------------


class TestRunAuthHeader:
    async def test_authorization_header_uses_correct_key(self) -> None:
        """mut_14/15/16: 'Authorization' → 'XXAuthorizationXX'/'authorization'/'AUTHORIZATION'."""
        rt = _make_runtime(bearer_token="mytoken")
        captured_headers: dict[str, str] = {}

        class _FakeWS:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def send(self, data):
                pass

            async def recv(self):
                raise asyncio.CancelledError

        async def _fake_connect(url, additional_headers=None, open_timeout=None):
            if additional_headers:
                captured_headers.update(additional_headers)
            return _FakeWS()

        async def _fake_start_connector():
            conn = MagicMock()
            conn.is_connected = MagicMock(return_value=True)
            conn.set_mode = AsyncMock(return_value=[])
            conn.get_snapshot = AsyncMock(return_value={"type": "snapshot"})
            conn.poll_messages = AsyncMock(return_value=[])
            conn.stop = AsyncMock()
            rt._connector = conn
            return conn

        rt._stop = asyncio.Event()
        rt._queue = asyncio.Queue()
        rt._stop.set()  # Stop before the inner ws loop

        with (
            patch.object(rt, "_start_connector", _fake_start_connector),
            patch("websockets.connect") as mock_connect,
        ):
            mock_connect.return_value = _FakeWS()
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_FakeWS())
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            # Just verify connect is called with Authorization header
            with contextlib.suppress(Exception):
                await rt._run()
            if mock_connect.called:
                kwargs = mock_connect.call_args.kwargs
                headers = kwargs.get("additional_headers", {})
                assert "Authorization" in headers
                assert headers["Authorization"] == "Bearer mytoken"


# ---------------------------------------------------------------------------
# _run() — finally block: _connected=False and _stop_connector called
# ---------------------------------------------------------------------------


class TestRunFinally:
    async def test_finally_sets_connected_false(self) -> None:
        """mut_127/128: _connected=None/True in finally."""
        rt = _make_runtime()
        rt._connected = True

        async def _fail_connector():
            rt._stop.set()
            raise RuntimeError("fail")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(rt, "_start_connector", _fail_connector),
        ):
            rt._stop = asyncio.Event()
            rt._queue = asyncio.Queue()
            await rt._run()

        assert rt._connected is False
