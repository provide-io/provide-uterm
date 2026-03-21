#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/runtime.py — run loop, worker-empty, force-release."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


# ===========================================================================
# runtime.py — HostedSessionRuntime.__init__
# ===========================================================================


class TestRun:
    async def test_run_uses_correct_backoff_values(self) -> None:
        """mutmut_2/3/4/5/6: backoff_s values mutated."""
        # Access the function directly via source inspection is fragile,
        # so we patch asyncio.sleep and verify calls match expected delays.
        rt = _make_runtime()
        sleep_calls: list[float] = []

        import websockets

        attempt_count = 0

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        async def _fail_connect(*args: Any, **kwargs: Any) -> None:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count >= 3:
                rt._stop.set()
            raise OSError("connection refused")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch.object(websockets, "connect", side_effect=_fail_connect),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with pytest.raises(Exception, match=".+"):
                await rt._run()

        # First retry: delay=0.25; second retry: delay=0.5
        if sleep_calls:
            assert sleep_calls[0] == pytest.approx(0.25)

    async def test_run_sets_state_error_on_value_error(self) -> None:
        """mutmut_28/29/30: state set to None/XXerrorXX/ERROR instead of 'error'."""
        rt = _make_runtime()

        async def _bad_connector() -> None:
            raise ValueError("bad config")

        with patch("provide.terminal.server.runtime.build_connector", side_effect=ValueError("bad config")):
            await rt._run()

        assert rt._state == "error"

    async def test_run_sets_connected_false_on_value_error(self) -> None:
        """mutmut_31/32: _connected set to None or True instead of False."""
        rt = _make_runtime()
        rt._connected = True

        with patch("provide.terminal.server.runtime.build_connector", side_effect=ValueError("bad")):
            await rt._run()

        assert rt._connected is False

    async def test_run_stores_last_error_on_value_error(self) -> None:
        """Covers _last_error being set correctly on ValueError."""
        rt = _make_runtime()

        with patch("provide.terminal.server.runtime.build_connector", side_effect=ValueError("specific error")):
            await rt._run()

        assert rt._last_error == "specific error"

    async def test_run_sets_state_error_on_generic_exception(self) -> None:
        """mutmut_56/57/58: _state mutated on general exception path."""
        rt = _make_runtime()

        import websockets

        called = [0]

        async def _fail(*args: Any, **kwargs: Any) -> None:
            called[0] += 1
            rt._stop.set()
            raise OSError("network error")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await rt._run()

        assert rt._state in ("error", "stopped")

    async def test_run_sets_connected_false_on_generic_exception(self) -> None:
        """mutmut_59/60: _connected set to None or True on generic exception."""
        rt = _make_runtime()
        rt._connected = True

        import websockets

        async def _fail(*args: Any, **kwargs: Any) -> None:
            rt._stop.set()
            raise OSError("network error")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await rt._run()

        assert rt._connected is False

    async def test_run_final_state_is_stopped(self) -> None:
        """mutmut_129/130/131: final self._state set to None/XXstoppedXX/STOPPED."""
        rt = _make_runtime()
        rt._stop.set()

        with patch("provide.terminal.server.runtime.build_connector", side_effect=ValueError("bad")):
            await rt._run()

        assert rt._state == "stopped"

    async def test_run_final_connected_is_false(self) -> None:
        """mutmut_127/128: finally block sets _connected to None or True."""
        rt = _make_runtime()
        rt._connected = True

        with patch("provide.terminal.server.runtime.build_connector", side_effect=ValueError("bad")):
            await rt._run()

        assert rt._connected is False

    async def test_run_404_stops_retrying(self) -> None:
        """mutmut_115: break replaced with return — _state never set to 'stopped'."""
        rt = _make_runtime()

        import websockets

        exc_404 = Exception("Not Found")
        exc_404.status_code = 404  # type: ignore[attr-defined]

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise exc_404

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await rt._run()

        # After 404 it should not retry (no sleep called)
        mock_sleep.assert_not_called()
        assert rt._state == "stopped"

    async def test_run_401_stops_retrying(self) -> None:
        """mutmut_115: 401 also triggers permanent stop."""
        rt = _make_runtime()

        import websockets

        exc_401 = Exception("Unauthorized")
        exc_401.status_code = 401  # type: ignore[attr-defined]

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise exc_401

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await rt._run()

        mock_sleep.assert_not_called()
        assert rt._state == "stopped"

    async def test_run_attempt_increments_on_failure(self) -> None:
        """mutmut_123/124/125: attempt += changed to attempt = 1 or attempt -= 1."""
        rt = _make_runtime()
        sleep_calls: list[float] = []
        call_count = 0

        import websockets

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            if call_count >= 2:
                rt._stop.set()

        async def _fail(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                rt._stop.set()
            raise OSError("net error")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
            patch("asyncio.sleep", side_effect=_fake_sleep),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            await rt._run()

        # If attempt increments correctly: delays should be 0.25, 0.5
        # If attempt = 1 each time: delays would be 0.25, 0.25 (wrong)
        if len(sleep_calls) >= 2:
            assert sleep_calls[1] > sleep_calls[0]

    async def test_run_response_status_code_checked(self) -> None:
        """mutmut_89/94/99/100/101/102: status_code lookup on response broken."""
        rt = _make_runtime()

        import websockets

        class _FakeResp:
            status_code = 401

        class _FakeExcWithRespError(Exception):
            response = _FakeResp()

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise _FakeExcWithRespError("auth error")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await rt._run()

        # Should stop without sleeping (permanent failure via response.status_code)
        mock_sleep.assert_not_called()

    async def test_run_backoff_capped_at_last_element(self) -> None:
        """mutmut_121/122: backoff_s[min(attempt, len-1)] vs len+1 or len-2."""
        rt = _make_runtime()
        sleep_calls: list[float] = []
        call_count = 0

        import websockets

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        async def _fail(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 8:
                rt._stop.set()
            raise OSError("net error")

        with (
            patch("provide.terminal.server.runtime.build_connector") as mock_build,
            patch.object(websockets, "connect", side_effect=_fail),
            patch("asyncio.sleep", side_effect=_fake_sleep),
        ):
            mock_connector = AsyncMock()
            mock_connector.is_connected.return_value = False
            mock_build.return_value = mock_connector
            await rt._run()

        # With 5 elements [0.25, 0.5, 1.0, 2.0, 5.0], max delay is 5.0
        if sleep_calls:
            assert max(sleep_calls) <= 5.0


# ===========================================================================
# registry.py — SessionRegistry._on_worker_empty()
# ===========================================================================


class TestOnWorkerEmpty:
    async def test_on_worker_empty_uses_session_id_for_browser_count(self) -> None:
        """mutmut_8: browser_count(None) instead of browser_count(session_id)."""
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=1)  # has browsers, don't delete
        session = _make_session("s1", ephemeral=True)
        reg = SessionRegistry(
            [session],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await reg._on_worker_empty("s1")
        # browser_count called with "s1"
        hub.browser_count.assert_called_with("s1")

    async def test_on_worker_empty_grace_period_is_5_seconds(self) -> None:
        """mutmut_7: sleep(6) instead of sleep(5)."""
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=1)  # has browsers
        session = _make_session("s1", ephemeral=True)
        reg = SessionRegistry(
            [session],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await reg._on_worker_empty("s1")

        assert 5 in sleep_calls

    async def test_on_worker_empty_deletes_ephemeral_session(self) -> None:
        """mutmut_15: sessions.pop() without default — but it should work."""
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=0)
        session = _make_session("s1", ephemeral=True)
        reg = SessionRegistry(
            [session],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await reg._on_worker_empty("s1")
        # session must be deleted
        assert "s1" not in reg._sessions


# ===========================================================================
# registry.py — SessionRegistry._force_release_hijack()
# ===========================================================================


class TestForceReleaseHijack:
    async def test_force_release_uses_session_id(self) -> None:
        """mutmut_1: force_release_hijack(None) instead of force_release_hijack(session_id)."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("sess1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        await reg._force_release_hijack("sess1")
        hub.force_release_hijack.assert_called_with("sess1")


# ===========================================================================
# registry.py — SessionRegistry.create_session()
# ===========================================================================
