#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests to close small coverage gaps across server package files."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from provide.terminal.server import create_server_app, default_server_config

# Capture the original default _api_key_store_hook at import time,
# before any test's create_server_app() replaces it.
import provide.terminal.server.auth as _auth_mod

_ORIGINAL_API_KEY_STORE_HOOK = _auth_mod._api_key_store_hook

# ---------------------------------------------------------------------------
# 1. tracing.py — lines 33-35 (set_attribute loop when span has set_attribute)
# ---------------------------------------------------------------------------


class TestTracingSetAttribute:
    """Cover the for-loop that calls set_attribute on each non-None attribute."""

    def test_span_calls_set_attribute_for_non_none_values(self) -> None:
        """When the span has a callable set_attribute, attributes are forwarded."""
        from provide.terminal.server.tracing import _SpanContext

        recorded: dict[str, str] = {}

        class FakeSpan:
            def set_attribute(self, key: str, value: str) -> None:
                recorded[key] = value

        class FakeCM:
            def __enter__(self) -> FakeSpan:
                return FakeSpan()

            def __exit__(self, *args: Any) -> None:
                pass

        ctx = _SpanContext(FakeCM(), {"key1": "val1", "key2": None, "key3": 42})
        with ctx as span:
            assert span is not None
        # key1 and key3 should be set; key2 (None) should be skipped
        assert recorded == {"key1": "val1", "key3": "42"}


# ---------------------------------------------------------------------------
# 2. auth.py — line 197 (default _api_key_store_hook returns None)
# ---------------------------------------------------------------------------


class TestAuthDefaultStoreHook:
    def test_default_api_key_store_hook_returns_none(self) -> None:
        """The original default _api_key_store_hook returns None (line 197).

        We call the original function captured at import time (before any
        test's create_server_app replaced it).
        """
        result = _ORIGINAL_API_KEY_STORE_HOOK()
        assert result is None


# ---------------------------------------------------------------------------
# 3. models.py — line 131 (TunnelConfig.token_ttl_s < 60 raises ValueError)
# ---------------------------------------------------------------------------


class TestTunnelConfigTtlValidation:
    def test_token_ttl_too_short_raises(self) -> None:
        from provide.terminal.server.models import TunnelConfig

        with pytest.raises(ValidationError, match="token_ttl_s must be >= 60"):
            TunnelConfig(token_ttl_s=59)


# ---------------------------------------------------------------------------
# 4. runtime.py — line 308 (break on CancelledError from done recv_task)
# ---------------------------------------------------------------------------


class TestRuntimeRecvTaskCancelled:
    async def test_bridge_session_breaks_on_cancelled_recv_task(self) -> None:
        """When a completed recv_task raises CancelledError, _bridge_session breaks."""
        from provide.terminal.server.models import RecordingConfig, SessionDefinition
        from provide.terminal.server.runtime import HostedSessionRuntime

        defn = SessionDefinition(
            session_id="test-cancel",
            connector_type="shell",
        )
        rt = HostedSessionRuntime(
            defn,
            public_base_url="http://localhost:9999",
            recording=RecordingConfig(),
        )

        # Build a fake connector
        connector = AsyncMock()
        connector.is_connected.return_value = True
        connector.set_mode = AsyncMock(return_value=[])
        connector.get_snapshot = AsyncMock(return_value={"type": "snapshot", "screen": "", "cursor": {"x": 0, "y": 0}})
        connector.poll_messages = AsyncMock(return_value=[])
        connector.stop = AsyncMock()
        rt._connector = connector
        rt._queue = asyncio.Queue(maxsize=2000)

        # Create a cancelled task to simulate recv_task that raises CancelledError
        async def _cancelled_coro() -> str:
            raise asyncio.CancelledError

        cancelled_task = asyncio.create_task(_cancelled_coro())
        # Wait for it to complete
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task

        # Create a fake ws whose recv returns a task that will be cancelled
        ws = AsyncMock()
        # First call to ws.recv will return a future that hangs (for the initial recv_task)
        # But we want to hit the path where recv_task is already done with CancelledError.
        # We'll simulate by having the queue be non-empty first (to enter the drain loop),
        # then when the queue is empty and recv_task is None, ws.recv() returns a cancelled future.

        call_count = 0

        async def _recv_side_effect() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError
            # Second call should not happen
            await asyncio.sleep(100)
            return ""

        ws.recv = _recv_side_effect
        ws.send = AsyncMock()

        # Set stop after a short delay to ensure we don't loop forever
        async def _set_stop() -> None:
            await asyncio.sleep(0.3)
            rt._stop.set()

        stop_task = asyncio.create_task(_set_stop())
        try:
            await rt._bridge_session(ws)
        finally:
            rt._stop.set()
            stop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop_task


# ---------------------------------------------------------------------------
# 5. rest_helpers.py — branch 35->37 (prompt_id is empty string)
# ---------------------------------------------------------------------------


class TestRestHelpersPromptIdBranch:
    def test_extract_prompt_id_empty_string(self) -> None:
        """extract_prompt_id returns None when prompt_id is an empty string."""
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        result = extract_prompt_id({"prompt_detected": {"prompt_id": ""}})
        assert result is None

    def test_extract_prompt_id_non_string(self) -> None:
        """extract_prompt_id returns None when prompt_id is not a string."""
        from provide.terminal.bridge.rest_helpers import extract_prompt_id

        result = extract_prompt_id({"prompt_detected": {"prompt_id": 123}})
        assert result is None


# ---------------------------------------------------------------------------
# 6. rest.py — lines 244-245 (compensating resume fails in finally block)
# ---------------------------------------------------------------------------


class TestHijackAcquireCompensatingResumeFails:
    async def test_compensating_resume_logs_warning_on_failure(self) -> None:
        """When the finally-block resume fails, the exception is caught and logged."""
        from provide.terminal.bridge.hub import TermHub

        hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
        router = hub.create_router()

        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Connect a worker so send_worker succeeds for the initial pause
        with client.websocket_connect("/ws/worker/comp-test/term") as ws:
            ws.send_text('{"type":"worker_hello","input_mode":"hijack"}')

            # Make try_acquire_rest_hijack raise an exception (simulating
            # client disconnect / CancelledError during the acquire).
            # The finally block will attempt a compensating resume.
            async def _raise_runtime(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("simulated failure")

            with patch.object(hub, "try_acquire_rest_hijack", side_effect=_raise_runtime):
                # Also make the compensating send_worker fail
                original_send = hub.send_worker

                call_count = 0

                async def _send_worker_failing(wid: str, msg: dict[str, Any]) -> bool:
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        # First call (pause) succeeds
                        return await original_send(wid, msg)
                    # Second call (compensating resume) fails
                    raise OSError("connection lost")

                with (
                    patch.object(hub, "send_worker", side_effect=_send_worker_failing),
                    pytest.raises(RuntimeError, match="simulated failure"),
                ):
                    # The RuntimeError from try_acquire propagates after the finally
                    # block runs (which covers lines 244-245). The test client raises
                    # it as-is, so we catch it here.
                    client.post("/worker/comp-test/hijack/acquire", json={"owner": "test"})


# ---------------------------------------------------------------------------
# 7. worker_link.py — branch 215->214 (cancelled task in _handle_connection)
#    and branch 320->322 (empty DataChunk in _recv_loop)
# ---------------------------------------------------------------------------


class TestWorkerLinkCancelledTask:
    async def test_handle_connection_cancelled_task_in_done(self) -> None:
        """When a done task is cancelled, _handle_connection skips it."""
        from provide.terminal.bridge.worker_link import TermBridge

        worker = MagicMock()
        worker.session = None
        worker.set_hijacked = AsyncMock()
        bridge = TermBridge(worker, "test-worker", "http://localhost:9999")

        ws = AsyncMock()

        # Make send_loop raise immediately, and recv_loop get cancelled
        async def _send_loop_error(ws_arg: Any) -> None:
            raise RuntimeError("send error")

        async def _recv_loop_hang(ws_arg: Any) -> None:
            await asyncio.sleep(100)

        with (
            patch.object(bridge, "_send_loop", _send_loop_error),
            patch.object(bridge, "_recv_loop", _recv_loop_hang),
            pytest.raises(RuntimeError, match="send error"),
        ):
            await bridge._handle_connection(ws)

    async def test_handle_connection_both_tasks_cancel(self) -> None:
        """When _handle_connection is cancelled, both tasks are cancelled."""
        from provide.terminal.bridge.worker_link import TermBridge

        worker = MagicMock()
        worker.session = None
        worker.set_hijacked = AsyncMock()
        bridge = TermBridge(worker, "test-worker", "http://localhost:9999")

        ws = AsyncMock()

        async def _hang(_ws: Any) -> None:
            await asyncio.sleep(100)

        with (
            patch.object(bridge, "_send_loop", _hang),
            patch.object(bridge, "_recv_loop", _hang),
        ):
            task = asyncio.create_task(bridge._handle_connection(ws))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestWorkerLinkEmptyDataChunk:
    async def test_recv_loop_empty_data_chunk_skipped(self) -> None:
        """Empty DataChunk in _recv_loop should not call _send_keys."""
        from provide.terminal.bridge.worker_link import TermBridge
        from provide.terminal.control_channel import encode_data

        worker = MagicMock()
        worker.session = None
        worker.set_hijacked = AsyncMock()
        bridge = TermBridge(worker, "test-worker", "http://localhost:9999")
        bridge._running = True

        # Prepare a ws mock that first yields an empty data frame, then closes
        empty_data_frame = encode_data("")
        call_count = 0

        async def _recv() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return empty_data_frame
            raise Exception("connection closed")

        ws = MagicMock()
        ws.recv = _recv
        ws.send = AsyncMock()

        send_keys_called = False
        original_send_keys = bridge._send_keys

        async def _tracking_send_keys(data: str) -> None:
            nonlocal send_keys_called
            send_keys_called = True
            await original_send_keys(data)

        with patch.object(bridge, "_send_keys", _tracking_send_keys):
            await bridge._recv_loop(ws)

        # _send_keys should NOT have been called for the empty data chunk
        assert not send_keys_called


# ---------------------------------------------------------------------------
# 8. routes/api_keys.py — lines 23, 93, 116
#    (missing principal, non-admin list, non-admin revoke)
# ---------------------------------------------------------------------------


class TestApiKeyRoutesMissingPrincipal:
    def test_missing_principal_raises_500(self) -> None:
        """When uterm_principal is not on request.state, _principal raises 500."""
        from provide.terminal.server.routes.api_keys import _principal

        request = MagicMock()
        request.state = MagicMock(spec=[])  # no uterm_principal attribute
        # Ensure getattr returns None
        assert getattr(request.state, "uterm_principal", None) is None
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _principal(request)
        assert exc_info.value.status_code == 500


class TestApiKeyRoutesNonAdminListAndRevoke:
    @pytest.fixture()
    def viewer_client(self) -> TestClient:
        """App with JWT auth where the test token grants 'viewer' role only."""
        import jwt as _jwt

        key = "uterm-test-secret-32-byte-minimum-key"
        now = int(time.time())
        self._viewer_token = _jwt.encode(
            {
                "sub": "viewer1",
                "roles": ["viewer"],
                "iss": "provide-terminal",
                "aud": "provide-terminal-server",
                "iat": now,
                "nbf": now,
                "exp": now + 600,
            },
            key=key,
            algorithm="HS256",
        )
        config = default_server_config()
        config.auth.mode = "jwt"
        config.auth.jwt_public_key_pem = key
        config.auth.worker_bearer_token = "worker-secret"
        config.auth.api_keys_enabled = True
        app = create_server_app(config)
        return TestClient(app)

    def test_viewer_cannot_list_keys(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(
            "/api/keys",
            headers={"Authorization": f"Bearer {self._viewer_token}"},
        )
        assert resp.status_code == 403

    def test_viewer_cannot_revoke_key(self, viewer_client: TestClient) -> None:
        resp = viewer_client.delete(
            "/api/keys/some-key-id",
            headers={"Authorization": f"Bearer {self._viewer_token}"},
        )
        assert resp.status_code == 403
