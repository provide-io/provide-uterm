#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/registry.py — SessionRegistry CRUD and modes."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.server.models import AuthConfig, RecordingConfig, SessionDefinition
from provide.terminal.server.registry import SessionRegistry, SessionValidationError
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


class TestCreateSession:
    async def test_create_session_visibility_operator_valid(self) -> None:
        """mutmut_54/55: 'operator' in set changed to 'XXoperatorXX' or 'OPERATOR'."""
        reg = _make_registry()
        status = await reg.create_session(
            {
                "session_id": "s1",
                "connector_type": "shell",
                "visibility": "operator",
            }
        )
        assert status.visibility == "operator"

    async def test_create_session_rejects_invalid_visibility(self) -> None:
        """Complement: invalid visibility raises."""
        reg = _make_registry()
        with pytest.raises((SessionValidationError, ValueError)):
            await reg.create_session(
                {
                    "session_id": "s2",
                    "connector_type": "shell",
                    "visibility": "superadmin",
                }
            )

    async def test_create_session_auto_start_defaults_to_false(self) -> None:
        """mutmut_76/105/107/110: auto_start field removed or default changed."""
        reg = _make_registry()
        await reg.create_session(
            {
                "session_id": "s1",
                "connector_type": "shell",
            }
        )
        # session must not be auto-started — task should not be running
        rt = reg._runtimes.get("s1")
        assert rt is not None
        assert rt._task is None or rt._task.done()

    async def test_create_session_auto_start_true_starts(self) -> None:
        """mutmut_110: auto_start=True default causes unintended auto-start."""
        reg = _make_registry()
        # Explicitly NOT passing auto_start — must default to False
        await reg.create_session({"session_id": "s2", "connector_type": "shell"})
        rt = reg._runtimes["s2"]
        # Task should not have been started
        assert not rt._task or rt._task.done()

    async def test_create_session_display_name_defaults_to_session_id(self) -> None:
        """mutmut_84/86: display_name default changed to None or nothing."""
        reg = _make_registry()
        await reg.create_session({"session_id": "my-id", "connector_type": "shell"})
        session = reg._sessions["my-id"]
        assert session.display_name == "my-id"

    async def test_create_session_display_name_uses_payload_value(self) -> None:
        """display_name from payload must override default."""
        reg = _make_registry()
        await reg.create_session({"session_id": "s1", "connector_type": "shell", "display_name": "My Custom Name"})
        assert reg._sessions["s1"].display_name == "My Custom Name"

    async def test_create_session_ephemeral_defaults_to_false(self) -> None:
        """mutmut_141/142/143/144/145/148: ephemeral default/source changed."""
        reg = _make_registry()
        await reg.create_session({"session_id": "s1", "connector_type": "shell"})
        assert reg._sessions["s1"].ephemeral is False

    async def test_create_session_ephemeral_true_from_payload(self) -> None:
        """mutmut_146/147: ephemeral key name changed to XXephemeralXX or EPHEMERAL."""
        reg = _make_registry()
        await reg.create_session({"session_id": "s1", "connector_type": "shell", "ephemeral": True})
        assert reg._sessions["s1"].ephemeral is True

    async def test_create_session_ephemeral_false_from_payload(self) -> None:
        """mutmut_148: default=True means ephemeral=True even when not in payload."""
        reg = _make_registry()
        # Explicitly set to False
        await reg.create_session({"session_id": "s1", "connector_type": "shell", "ephemeral": False})
        assert reg._sessions["s1"].ephemeral is False

    async def test_create_session_invalid_connector_raises(self) -> None:
        """mutmut_23: error msg changed to None."""
        reg = _make_registry()
        with pytest.raises((SessionValidationError, Exception)):
            await reg.create_session({"session_id": "s1", "connector_type": "ftp"})

    async def test_create_session_visibility_public_valid(self) -> None:
        """mutmut_134/138/140: visibility cast mutated."""
        reg = _make_registry()
        status = await reg.create_session({"session_id": "s1", "connector_type": "shell", "visibility": "public"})
        assert status.visibility == "public"

    async def test_create_session_visibility_private_valid(self) -> None:
        reg = _make_registry()
        status = await reg.create_session({"session_id": "s1", "connector_type": "shell", "visibility": "private"})
        assert status.visibility == "private"


# ===========================================================================
# registry.py — SessionRegistry.update_session()
# ===========================================================================


class TestUpdateSession:
    async def test_update_session_model_dump_mode_python(self) -> None:
        """mutmut_7/8/9: model_dump mode changed to None/XXpythonXX/PYTHON."""
        reg = _make_registry([_make_session("s1")])
        # update with valid input_mode — will fail if model_dump returns wrong types
        status = await reg.update_session("s1", {"display_name": "Updated"})
        assert status is not None

    async def test_update_session_input_mode_triggers_set_mode(self) -> None:
        """mutmut_24/25/26: input_mode key check mutated."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        # set_mode on runtime should be called when input_mode in updates
        rt = reg._runtimes.get("s1") or reg._runtime_for(reg._sessions["s1"])
        set_mode_calls: list[str] = []

        original_set_mode = rt.set_mode

        async def _capture_set_mode(mode: str) -> None:
            set_mode_calls.append(mode)
            await original_set_mode(mode)

        rt.set_mode = _capture_set_mode  # type: ignore[method-assign]
        reg._runtimes["s1"] = rt

        await reg.update_session("s1", {"input_mode": "hijack"})
        assert "hijack" in set_mode_calls

    async def test_update_session_no_input_mode_no_set_mode(self) -> None:
        """mutmut_26: 'input_mode' not in updates — set_mode not called."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        # patch set_mode to verify it's NOT called
        rt = reg._runtime_for(reg._sessions["s1"])
        set_mode_calls: list[str] = []
        original = rt.set_mode

        async def _capture(mode: str) -> None:
            set_mode_calls.append(mode)
            await original(mode)

        rt.set_mode = _capture  # type: ignore[method-assign]
        reg._runtimes["s1"] = rt

        await reg.update_session("s1", {"display_name": "no mode change"})
        assert set_mode_calls == []


# ===========================================================================
# registry.py — SessionRegistry.delete_session()
# ===========================================================================


class TestDeleteSession:
    async def test_delete_session_removes_from_sessions(self) -> None:
        """mutmut_3: sessions.pop without default — still should work."""
        reg = _make_registry([_make_session("s1")])
        await reg.delete_session("s1")
        assert "s1" not in reg._sessions

    async def test_delete_session_stops_runtime(self) -> None:
        """mutmut_4: runtime = None instead of _runtimes.pop — runtime not stopped."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        # Ensure runtime is created
        rt = reg._runtime_for(reg._sessions["s1"])
        stop_called = False
        original_stop = rt.stop

        async def _capture_stop() -> None:
            nonlocal stop_called
            stop_called = True
            await original_stop()

        rt.stop = _capture_stop  # type: ignore[method-assign]
        reg._runtimes["s1"] = rt

        await reg.delete_session("s1")
        assert stop_called

    async def test_delete_session_uses_session_id_for_runtimes_pop(self) -> None:
        """mutmut_5: _runtimes.pop(None, None) instead of pop(session_id, None)."""
        reg = _make_registry([_make_session("s1")])
        # Create a runtime for s1
        _ = reg._runtime_for(reg._sessions["s1"])
        assert "s1" in reg._runtimes
        await reg.delete_session("s1")
        assert "s1" not in reg._runtimes


# ===========================================================================
# registry.py — SessionRegistry.set_mode()
# ===========================================================================


class TestRegistrySetMode:
    async def test_set_mode_model_dump_python(self) -> None:
        """mutmut_5/6/7: model_dump mode mutated."""
        reg = _make_registry([_make_session("s1")])
        status = await reg.set_mode("s1", "hijack")
        assert status is not None

    async def test_set_mode_assigns_validated_input_mode(self) -> None:
        """mutmut_12: session.input_mode = None instead of validated.input_mode."""
        reg = _make_registry([_make_session("s1")])
        await reg.set_mode("s1", "hijack")
        assert reg._sessions["s1"].input_mode == "hijack"

    async def test_set_mode_open_releases_hijack(self) -> None:
        """mutmut_15/16/17: condition mutated — hijack not released on open."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        await reg.set_mode("s1", "open")
        hub.force_release_hijack.assert_called_with("s1")

    async def test_set_mode_hijack_does_not_release_hijack(self) -> None:
        """mutmut_15 (inverted): hijack mode should NOT release."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        await reg.set_mode("s1", "hijack")
        hub.force_release_hijack.assert_not_called()

    async def test_set_mode_force_release_uses_session_id(self) -> None:
        """mutmut_18: force_release_hijack(None) instead of (session_id)."""
        hub = _make_hub()
        reg = SessionRegistry(
            [_make_session("s1")],
            hub=hub,
            public_base_url="http://localhost",
            recording=RecordingConfig(),
        )
        await reg.set_mode("s1", "open")
        hub.force_release_hijack.assert_called_with("s1")


# ===========================================================================
# registry.py — SessionRegistry.last_snapshot(), events()
# ===========================================================================


class TestLastSnapshotAndEvents:
    async def test_last_snapshot_uses_session_id(self) -> None:
        """mutmut_1: get_last_snapshot(None) instead of get_last_snapshot(session_id)."""
        hub = _make_hub()
        hub.get_last_snapshot = AsyncMock(return_value={"type": "snapshot"})
        reg = _make_registry(hub=hub)
        await reg.last_snapshot("my-session")
        hub.get_last_snapshot.assert_called_with("my-session")

    async def test_events_default_limit_is_100(self) -> None:
        """mutmut_1: default limit=101 instead of 100."""
        hub = _make_hub()
        hub.get_recent_events = AsyncMock(return_value=[])
        reg = _make_registry(hub=hub)
        # Call without explicit limit — should use default 100
        await reg.events("some-session")
        hub.get_recent_events.assert_called_with("some-session", 100)

    async def test_events_uses_session_id(self) -> None:
        """mutmut_2: get_recent_events(None, limit) instead of (session_id, limit)."""
        hub = _make_hub()
        hub.get_recent_events = AsyncMock(return_value=[])
        reg = _make_registry(hub=hub)
        await reg.events("target-session", limit=50)
        hub.get_recent_events.assert_called_with("target-session", 50)

    async def test_events_passes_limit(self) -> None:
        """mutmut_3: get_recent_events(session_id, None) instead of (session_id, limit)."""
        hub = _make_hub()
        hub.get_recent_events = AsyncMock(return_value=[])
        reg = _make_registry(hub=hub)
        await reg.events("s", limit=42)
        hub.get_recent_events.assert_called_with("s", 42)
