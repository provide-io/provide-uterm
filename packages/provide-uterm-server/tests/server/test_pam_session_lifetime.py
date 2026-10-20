#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PAM sessions outlive their last browser viewer.

A PAM session mirrors a live login. It used to be created ``ephemeral``, so when
the last browser left the registry deleted it 5 s later and stopped its runtime,
which unlinked the capture socket: the login's session could never be opened
again. A PAM login owns its session: only the PAM ``close`` event removes it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")

from provide.uterm.pty.pam_listener import PamEvent
from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server import pam_integration
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.config_schema import PamConfig, RecordingConfig
from provide.uterm.server.registry import SessionRegistry

# The capture id carries the sshd process's start time; pid 4242's is pinned.
SESSION_ID = "pam-suokki-capture-4242-777"


@pytest.fixture(autouse=True)
def _pinned_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pam_integration, "_process_start_ticks", lambda pid: 777 if pid == 4242 else None)


class _Env:
    def __init__(self, hub: TermHub, registry: SessionRegistry, sock: str, directory: Path) -> None:
        self.hub = hub
        self.registry = registry
        self.sock = sock
        self.directory = directory


@pytest.fixture
def env(tmp_path: Path) -> Any:
    hub = TermHub()
    rec = RecordingConfig(directory=tmp_path)
    registry = SessionRegistry(
        [],
        hub=hub,
        public_base_url="http://127.0.0.1:9",
        recording=rec,
        recording_store=LocalFileRecordingStore(rec.directory),
    )
    return _Env(hub, registry, str(tmp_path / "c.sock"), tmp_path)


@pytest.fixture
def fast_grace(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make the registry's 5 s grace period instant, recording what it asked for."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


async def _open_capture(env: _Env, cfg: PamConfig) -> None:
    ev = PamEvent(event="open", username="suokki", tty="ssh", pid=4242, mode="capture", capture_socket=env.sock)
    await pam_integration._create_capture_session(ev, cfg, env.registry)
    # A deadline, not a count of sleeps: fast_grace makes asyncio.sleep yield
    # only, and the socket waits on a probe run on a thread.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if Path(env.sock).exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("capture socket never appeared")


async def _last_browser_leaves(env: _Env) -> None:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    async with env.hub._lock:
        state = env.hub.registry.get(SESSION_ID) or WorkerTermState()
        state.browsers[ws] = "admin"
        env.hub.registry._workers[SESSION_ID] = state
    await env.hub.cleanup_browser_disconnect(SESSION_ID, ws, False)
    # The hub fires the registry's empty-worker callback as a background task.
    await asyncio.gather(*list(env.hub._background_tasks))


async def _teardown(env: _Env) -> None:
    for sid in list(env.registry._runtimes):
        await env.registry.delete_session(sid)


async def test_capture_session_survives_its_last_viewer(env: _Env, fast_grace: list[float]) -> None:
    await _open_capture(env, PamConfig(mode="capture", capture_socket_dir=str(env.directory)))
    try:
        await _last_browser_leaves(env)
        await asyncio.sleep(0.2)

        assert await env.registry.get_definition(SESSION_ID) is not None
        assert SESSION_ID in env.registry._runtimes
        assert Path(env.sock).exists()
        assert 5 not in fast_grace  # nothing waited to delete it
    finally:
        await _teardown(env)


async def test_pam_close_still_removes_a_surviving_session(env: _Env, fast_grace: list[float]) -> None:
    cfg = PamConfig(mode="capture", capture_socket_dir=str(env.directory))
    await _open_capture(env, cfg)
    try:
        await _last_browser_leaves(env)
        ev = PamEvent(event="close", username="suokki", tty="ssh", pid=4242, mode="capture")

        await pam_integration._on_close(ev, cfg, env.registry)

        assert await env.registry.get_definition(SESSION_ID) is None
        assert SESSION_ID not in env.registry._runtimes
        for _ in range(100):
            if not Path(env.sock).exists():
                break
            await asyncio.sleep(0.05)
        assert not Path(env.sock).exists()
    finally:
        await _teardown(env)


# ── the flag follows the setting on every PAM path ────────────────────────────


async def test_capture_payload_is_not_ephemeral() -> None:
    registry = MagicMock()
    registry.create_session = AsyncMock()
    ev = PamEvent(event="open", username="a", tty="/dev/pts/3", pid=1, mode="capture", capture_socket="/run/c.sock")

    await pam_integration._on_open(ev, PamConfig(mode="capture"), registry)

    assert registry.create_session.call_args[0][0]["ephemeral"] is False


async def test_notify_payload_is_not_ephemeral() -> None:
    registry = MagicMock()
    registry.create_session = AsyncMock()
    ev = PamEvent(event="open", username="b", tty="/dev/pts/7", pid=2)

    await pam_integration._on_open(ev, PamConfig(auto_session=True), registry)

    assert registry.create_session.call_args[0][0]["ephemeral"] is False


async def test_relay_route_payload_is_not_ephemeral() -> None:
    from provide.uterm.server.routes.pam_events import pam_event_capability_handlers

    registry = MagicMock()
    registry.create_session = AsyncMock()
    authz = MagicMock()
    authz.can_create_session = AsyncMock(return_value=True)
    request = MagicMock()
    request.app.state = SimpleNamespace(uterm_registry=registry, uterm_authz=authz)
    request.state = SimpleNamespace(uterm_principal=SimpleNamespace(subject_id="a", roles=set()))
    request.json = AsyncMock(return_value={"event": "open", "username": "alice", "tty": "/dev/pts/3"})

    await pam_event_capability_handlers()["pam_events.ingest"](request)

    assert registry.create_session.call_args[0][0]["ephemeral"] is False
