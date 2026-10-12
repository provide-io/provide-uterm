#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server import pam_integration
from provide.uterm.server.pam_integration import (
    _on_close,
    _on_open,
    _session_id,
    run_pam_integration,
)

# ── _tty_slug ─────────────────────────────────────────────────────────────────


def test_pam_config_relay_fields_default_none() -> None:
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig()
    assert cfg.relay_url is None
    assert cfg.relay_token is None


async def test_forward_to_relay_posts_event() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_response = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _forward_to_relay(
            {"event": "open", "username": "alice", "pid": 1},
            "https://cf.example.com",
            "tok-abc",
        )

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://cf.example.com/api/pam-events"
    assert call_args[1]["headers"]["Authorization"] == "Bearer tok-abc"
    assert call_args[1]["json"]["username"] == "alice"


async def test_forward_to_relay_trailing_slash_stripped() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "close"}, "https://cf.example.com/", "tok")

    url = mock_client.post.call_args[0][0]
    assert url == "https://cf.example.com/api/pam-events"


async def test_forward_to_relay_swallows_network_error() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx2

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx2.ConnectError("unreachable"))

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "open"}, "https://x.example.com", "tok")  # must not raise


async def test_create_relay_tunnel_returns_token_and_endpoint() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={"worker_token": "wt-123", "ws_endpoint": "wss://cf.example.com/tunnel/abc"}
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx2.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "pam-alice-3", "alice (/dev/pts/3)")

    assert result == ("wt-123", "wss://cf.example.com/tunnel/abc")
    body = mock_client.post.call_args[1]["json"]
    assert body["session_id"] == "pam-alice-3"
    assert body["tunnel_type"] == "terminal"


async def test_create_relay_tunnel_returns_none_on_error() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx2

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx2.ConnectError("unreachable"))

    with patch("httpx2.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "s1", "name")

    assert result is None


async def test_on_open_forwards_to_cf_when_configured() -> None:
    """_on_open calls _forward_to_relay when relay_url + relay_token are set."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.pty.pam_listener import PamEvent
    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.create_session = AsyncMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
        )
    )

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _on_open(ev, cfg, registry)

    assert mock_client.post.await_count >= 1


async def test_on_close_forwards_to_cf_when_configured() -> None:
    """_on_close calls _forward_to_relay when relay_url + relay_token are set."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.pty.pam_listener import PamEvent
    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=None)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _on_close(ev, cfg, registry)

    mock_client.post.assert_awaited_once()
    body = mock_client.post.call_args[1]["json"]
    assert body["event"] == "close"
    assert body["username"] == "alice"


# ── _session_id ───────────────────────────────────────────────────────────────


def test_session_id_with_tty_uses_slug_only() -> None:
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.pty.pam_listener import PamEvent

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/3", pid=1234)
    assert _session_id(ev) == "pam-alice-3"


@pytest.fixture
def start_ticks(monkeypatch: pytest.MonkeyPatch) -> dict[int, int | None]:
    """Pin each pid's process start time; a pid left out reads as unreadable."""
    ticks: dict[int, int | None] = {}
    monkeypatch.setattr(pam_integration, "_process_start_ticks", ticks.get)
    return ticks


def test_session_id_empty_tty_includes_pid(start_ticks: dict[int, int | None]) -> None:
    """Empty TTY must include PID to prevent collision between concurrent sessions."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.pty.pam_listener import PamEvent

    start_ticks.update({100: 5100, 200: 5200})
    ev1 = PamEvent(event="open", username="alice", tty="", pid=100)
    ev2 = PamEvent(event="open", username="alice", tty="", pid=200)
    assert _session_id(ev1) != _session_id(ev2)
    assert _session_id(ev1) == "pam-alice-tty-100-5100"
    assert _session_id(ev2) == "pam-alice-tty-200-5200"


def test_session_id_open_and_close_match_with_same_pid(start_ticks: dict[int, int | None]) -> None:
    """Open and close of one live process with an empty TTY map to the same session_id."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.pty.pam_listener import PamEvent

    start_ticks[999] = 4321
    ev_open = PamEvent(event="open", username="bob", tty="", pid=999)
    ev_close = PamEvent(event="close", username="bob", tty="", pid=999)
    assert _session_id(ev_open) == _session_id(ev_close)


def _capture_event(pid: int, event: str = "open") -> object:
    from provide.uterm.pty.pam_listener import PamEvent

    return PamEvent(
        event=event,  # type: ignore[arg-type]
        username="suokki",
        tty="ssh",
        pid=pid,
        mode="capture",
        capture_socket=f"/run/uterm-cap-{pid}.sock" if event == "open" else None,
    )


def test_a_recycled_pid_gets_a_different_capture_id(start_ticks: dict[int, int | None]) -> None:
    """Two logins that draw the same sshd pid are two sessions, never one id."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")

    start_ticks[62738] = 1000
    first = _session_id(_capture_event(62738))  # type: ignore[arg-type]
    start_ticks[62738] = 98000  # the pid came round again, to a later process
    second = _session_id(_capture_event(62738))  # type: ignore[arg-type]
    assert first == "pam-suokki-capture-62738-1000"
    assert second == "pam-suokki-capture-62738-98000"


def test_an_unreadable_start_time_falls_back_to_a_random_tag(start_ticks: dict[int, int | None]) -> None:
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    import re

    first = _session_id(_capture_event(7))  # type: ignore[arg-type]
    second = _session_id(_capture_event(7))  # type: ignore[arg-type]
    assert re.fullmatch(r"pam-suokki-capture-7-r[0-9a-f]{8}", first)
    assert first != second


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_process_start_ticks_reads_proc_stat() -> None:
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().rpartition(")")[2].split()
    assert pam_integration._process_start_ticks(os.getpid()) == int(fields[19])
    assert pam_integration._process_start_ticks(0) is None
    assert pam_integration._process_start_ticks(-1) is None
    assert pam_integration._process_start_ticks(2**22 + 12345) is None  # above any pid_max


async def test_close_finds_the_id_open_gave_even_after_the_process_is_gone(
    start_ticks: dict[int, int | None],
) -> None:
    """The close must not depend on reading /proc: sshd may already have exited."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()
    registry.delete_session = AsyncMock()
    instances: dict[tuple[str, int], str] = {}

    start_ticks[4242] = 31337
    await _on_open(_capture_event(4242), cfg, registry, None, instances)  # type: ignore[arg-type]
    created = registry.create_session.call_args[0][0]["session_id"]
    assert created == "pam-suokki-capture-4242-31337"

    del start_ticks[4242]  # the sshd process has exited by the time close is handled
    await _on_close(_capture_event(4242, "close"), cfg, registry, None, instances)  # type: ignore[arg-type]
    registry.delete_session.assert_awaited_once_with(created)
    assert instances == {}


async def test_open_never_reuses_an_id_a_closeless_login_left(start_ticks: dict[int, int | None]) -> None:
    """A login drawing a pid whose earlier login never saw its close is a new session."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()
    instances: dict[tuple[str, int], str] = {}

    start_ticks[9] = 100
    await _on_open(_capture_event(9), cfg, registry, None, instances)  # type: ignore[arg-type]
    start_ticks[9] = 200  # no close came; the pid is now a later login's
    await _on_open(_capture_event(9), cfg, registry, None, instances)  # type: ignore[arg-type]
    ids = [c.args[0]["session_id"] for c in registry.create_session.call_args_list]
    assert ids == ["pam-suokki-capture-9-100", "pam-suokki-capture-9-200"]
    assert instances == {("suokki", 9): "pam-suokki-capture-9-200"}


async def test_a_login_whose_close_never_came_is_forgotten_after_the_grace(
    start_ticks: dict[int, int | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.server.models import PamConfig

    clock = [1000.0]
    monkeypatch.setattr(pam_integration, "_monotonic", lambda: clock[0])
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()
    instances = pam_integration.OpenLogins()

    start_ticks.update({10: 1, 11: 2, 12: 3, 13: 4})
    for pid in (10, 11):
        await _on_open(_capture_event(pid), cfg, registry, None, instances)  # type: ignore[arg-type]
    start_ticks[10] = 5  # pid 10's sshd exited without a close; the pid is someone else's now
    await _on_open(_capture_event(12), cfg, registry, None, instances)  # type: ignore[arg-type]
    assert ("suokki", 10) in instances  # seen gone, but not for long enough
    clock[0] += 61
    await _on_open(_capture_event(13), cfg, registry, None, instances)  # type: ignore[arg-type]
    assert sorted(instances) == [("suokki", 11), ("suokki", 12), ("suokki", 13)]
    assert instances.gone_since == {}


async def test_a_close_in_flight_still_finds_its_entry_after_another_event_prunes(
    start_ticks: dict[int, int | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """pam_uterm.so sends the close and exits; another login's event runs first."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.server.models import PamConfig

    clock = [1000.0]
    monkeypatch.setattr(pam_integration, "_monotonic", lambda: clock[0])
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()
    registry.delete_session = AsyncMock()
    instances = pam_integration.OpenLogins()

    start_ticks.update({30: 7, 31: 8})
    await _on_open(_capture_event(30), cfg, registry, None, instances)  # type: ignore[arg-type]
    opened = registry.create_session.call_args[0][0]["session_id"]
    del start_ticks[30]  # pid 30's PAM process exits right after sending its close
    await _on_open(_capture_event(31), cfg, registry, None, instances)  # type: ignore[arg-type]  # prunes first
    clock[0] += 5
    await _on_close(_capture_event(30, "close"), cfg, registry, None, instances)  # type: ignore[arg-type]
    registry.delete_session.assert_awaited_once_with(opened)


async def test_logins_that_cannot_be_proven_gone_are_capped(
    start_ticks: dict[int, int | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.server.models import PamConfig

    monkeypatch.setattr(pam_integration, "_MAX_REMEMBERED_LOGINS", 3)
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()
    instances: dict[tuple[str, int], str] = {}
    for pid in range(20, 25):  # no start times: random tags, nothing provably gone
        await _on_open(_capture_event(pid), cfg, registry, None, instances)  # type: ignore[arg-type]
    assert [pid for _, pid in instances] == [22, 23, 24]


def test_capture_session_id_uses_pid_when_openssh_reports_placeholder_tty(
    start_ticks: dict[int, int | None],
) -> None:
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.pty.pam_listener import PamEvent

    first = PamEvent(
        event="open",
        username="alice",
        tty="ssh",
        pid=100,
        mode="capture",
        capture_socket="/run/uterm-cap-100.sock",
    )
    second = PamEvent(
        event="open",
        username="alice",
        tty="ssh",
        pid=200,
        mode="capture",
        capture_socket="/run/uterm-cap-200.sock",
    )

    start_ticks.update({100: 5100, 200: 5200})
    assert _session_id(first) == "pam-alice-capture-100-5100"
    assert _session_id(second) == "pam-alice-capture-200-5200"


# ── run_pam_integration event loop ───────────────────────────────────────────


async def test_run_pam_integration_dispatches_event_via_real_socket() -> None:
    """Full integration: real Unix socket → run_pam_integration → handler called."""
    import asyncio
    import json
    import tempfile
    from pathlib import Path

    try:
        import provide.uterm.pty.pam_listener  # noqa: F401
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig, ServerConfig

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "pam-notify.sock")
        config = ServerConfig(pam=PamConfig(notify_socket=sock_path, auto_session=True))

        registry = MagicMock()
        registry.create_session = AsyncMock()

        # Start integration in background task
        task = asyncio.create_task(run_pam_integration(config, registry))
        # Give the listener time to bind
        await asyncio.sleep(0.05)

        # Send a real PAM open event over the socket
        event_line = (
            json.dumps(
                {
                    "event": "open",
                    "username": "testuser",
                    "tty": "/dev/pts/5",
                    "pid": 7777,
                }
            ).encode()
            + b"\n"
        )
        reader, writer = await asyncio.open_unix_connection(sock_path)
        writer.write(event_line)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        # Wait for handler to be called
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if registry.create_session.await_count >= 1:
                break
            await asyncio.sleep(0.05)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        registry.create_session.assert_awaited_once()
        payload = registry.create_session.call_args[0][0]
        assert payload["session_id"] == "pam-testuser-5"
        assert payload["connector_config"]["username"] == "testuser"


async def test_run_pam_integration_cancelled_cleanly() -> None:
    """Cancelling run_pam_integration does not raise outside CancelledError."""
    import asyncio
    import tempfile
    from pathlib import Path

    from provide.uterm.server.models import PamConfig, ServerConfig

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "pam-cancel.sock")
        config = ServerConfig(pam=PamConfig(notify_socket=sock_path))
        registry = MagicMock()

        task = asyncio.create_task(run_pam_integration(config, registry))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Socket should be cleaned up
        assert not Path(sock_path).exists()


# ── bridge error path ─────────────────────────────────────────────────────────


async def test_on_open_bridge_start_failure_cleans_up() -> None:
    """If PamTunnelBridge.start() raises, bridge.stop() is called for cleanup."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.pty.pam_listener import PamEvent
    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.create_session = AsyncMock()
    runtime = MagicMock()
    connector = MagicMock()
    runtime.connector = connector
    registry.get_runtime = MagicMock(return_value=runtime)

    bridge_mock = MagicMock()
    bridge_mock.start = AsyncMock(side_effect=RuntimeError("connect failed"))
    bridge_mock.stop = AsyncMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
        )
    )

    bridges: dict[str, object] = {}
    with (
        patch("httpx2.AsyncClient", return_value=mock_client),
        patch("provide.uterm.server.pam_tunnel.PamTunnelBridge", return_value=bridge_mock),
    ):
        await _on_open(ev, cfg, registry, bridges)

    # bridge.start() raised → bridge not stored
    assert "pam-alice-0" not in bridges
    # bridge.stop() called to clean up partial state
    bridge_mock.stop.assert_awaited_once()


# ── L11: relay egress guard ───────────────────────────────────────────────────


async def test_forward_to_relay_blocked_for_metadata_ip_no_post() -> None:
    """A relay_url whose host is a cloud-metadata IP must be blocked before the
    POST — exfiltrating PAM event data + the relay bearer token to 169.254.169.254
    is refused by the egress guard, and the PAM loop does not crash."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx2.AsyncClient", return_value=mock_client):
        # Literal metadata IP → assert_webhook_target_allowed raises EgressBlockedError.
        await _forward_to_relay({"event": "open"}, "https://169.254.169.254/", "tok")  # must not raise

    # Egress guard fired before the POST → no event/token left the host.
    mock_client.post.assert_not_awaited()


async def test_forward_to_relay_allows_benign_url_posts() -> None:
    """A benign relay_url passes the egress guard and the POST proceeds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx2.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "open"}, "https://cf.example.com", "tok")

    mock_client.post.assert_awaited_once()


async def test_create_relay_tunnel_blocked_for_metadata_ip_returns_none() -> None:
    """A metadata-IP relay_url blocks the tunnel POST and returns None, mirroring
    the existing failure handling — the loop does not crash."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx2.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://169.254.169.254/", "tok", "s1", "name")

    assert result is None
    mock_client.post.assert_not_awaited()


async def test_create_relay_tunnel_allows_benign_url() -> None:
    """A benign relay_url passes the egress guard; the tunnel POST proceeds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"worker_token": "wt", "ws_endpoint": "wss://x"})
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx2.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "s1", "name")

    assert result == ("wt", "wss://x")
    mock_client.post.assert_awaited_once()
