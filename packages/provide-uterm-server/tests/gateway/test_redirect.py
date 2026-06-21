#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the redirect-follow feature: _apply_redirect, _run_gateway_session,
and the _handle_ws_control_frame redirect branch."""

from __future__ import annotations

from unittest.mock import AsyncMock

from provide.uterm.gateway._gateway import (
    _apply_redirect,
    _handle_ws_control_frame,
    _run_gateway_session,
)

# ---------------------------------------------------------------------------
# _apply_redirect
# ---------------------------------------------------------------------------


class TestApplyRedirect:
    def test_same_origin_swap(self) -> None:
        result = _apply_redirect("ws://host:8787/ws/terminal", "/ws/other?x=1")
        assert result == "ws://host:8787/ws/other?x=1"

    def test_scheme_and_host_preserved(self) -> None:
        result = _apply_redirect("wss://example.com/ws/terminal", "/ws/game/A")
        assert result == "wss://example.com/ws/game/A"

    def test_query_carried_through(self) -> None:
        result = _apply_redirect("ws://host/ws/t", "/ws/other?token=abc&x=1")
        assert result == "ws://host/ws/other?token=abc&x=1"

    def test_rejects_protocol_relative(self) -> None:
        assert _apply_redirect("ws://host/ws/t", "//evil.com/x") is None

    def test_rejects_absolute_url(self) -> None:
        assert _apply_redirect("ws://host/ws/t", "https://evil.com/x") is None

    def test_rejects_relative_path(self) -> None:
        assert _apply_redirect("ws://host/ws/t", "relative/path") is None

    def test_rejects_empty_string(self) -> None:
        assert _apply_redirect("ws://host/ws/t", "") is None

    def test_rejects_just_slash_slash(self) -> None:
        assert _apply_redirect("ws://host/ws/t", "//other") is None


# ---------------------------------------------------------------------------
# _handle_ws_control_frame redirect branch
# ---------------------------------------------------------------------------


class TestHandleWsControlFrameRedirect:
    async def test_redirect_sets_holder(self) -> None:
        redirect_holder: list[str | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "redirect", "path": "/ws/game/B"},
            [None],  # token_holder
            write_fn,
            redirect_holder=redirect_holder,
        )
        assert result is True
        assert redirect_holder[0] == "/ws/game/B"

    async def test_redirect_noop_when_holder_is_none(self) -> None:
        write_fn = AsyncMock()
        # redirect_holder=None means the caller doesn't support redirect — no crash
        result = await _handle_ws_control_frame(
            {"type": "redirect", "path": "/ws/game/B"},
            [None],  # token_holder
            write_fn,
            redirect_holder=None,
        )
        assert result is True  # frame was handled (no-op), True returned

    async def test_redirect_ignores_non_string_path(self) -> None:
        redirect_holder: list[str | None] = [None]
        write_fn = AsyncMock()
        result = await _handle_ws_control_frame(
            {"type": "redirect", "path": 42},
            [None],
            write_fn,
            redirect_holder=redirect_holder,
        )
        assert result is False  # not handled — path isn't a string
        assert redirect_holder[0] is None

    async def test_backward_compat_no_redirect_holder(self) -> None:
        """Callers without redirect_holder param still work."""
        holder: list[dict | None] = [None]
        write_fn = AsyncMock()
        # Should not crash; session_token still handled
        result = await _handle_ws_control_frame(
            {"type": "session_token", "token": "t1"},
            holder,
            write_fn,
        )
        assert result is True
        assert holder[0] == {"token": "t1"}


# ---------------------------------------------------------------------------
# _run_gateway_session
# ---------------------------------------------------------------------------


class TestRunGatewaySession:
    async def test_redirect_follows_immediately_no_sleep(self) -> None:
        """Redirect → pump called again with swapped URL, no sleep."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            if url == "ws://host/ws/old":
                redirect_holder[0] = "/ws/new"
            else:
                redirect_holder[0] = None
            return None  # transient close

        show_reconnecting_calls: list[None] = []

        async def show_reconnecting() -> None:
            show_reconnecting_calls.append(None)

        await _run_gateway_session(
            ws_url="ws://host/ws/old",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=lambda: len(calls) < 2,
            show_reconnecting=show_reconnecting,
            max_reconnects=5,
            reconnect_delay=3.0,
            max_redirects=5,
        )

        assert calls[0] == "ws://host/ws/old"
        assert calls[1] == "ws://host/ws/new"
        assert not show_reconnecting_calls  # no show_reconnecting on redirect

    async def test_redirect_cap_stops_loop(self) -> None:
        """Too many redirects → loop breaks."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            redirect_holder[0] = "/ws/loop"  # always redirect
            return None

        async def show_reconnecting() -> None:
            pass

        await _run_gateway_session(
            ws_url="ws://host/ws/start",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=lambda: True,
            show_reconnecting=show_reconnecting,
            max_reconnects=5,
            reconnect_delay=0.0,
            max_redirects=3,
        )

        # Should stop after max_redirects+1 pump calls
        assert len(calls) == 4  # initial + 3 redirects

    async def test_cross_origin_redirect_rejected(self) -> None:
        """Cross-origin path → loop breaks immediately."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            redirect_holder[0] = "//evil.com/x"
            return None

        async def show_reconnecting() -> None:
            pass

        await _run_gateway_session(
            ws_url="ws://host/ws/start",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=lambda: True,
            show_reconnecting=show_reconnecting,
            max_reconnects=5,
            reconnect_delay=0.0,
            max_redirects=5,
        )

        assert len(calls) == 1  # rejected on first redirect

    async def test_close_code_1000_stops_loop(self) -> None:
        """WS close 1000 → deliberate close, no reconnect."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            return 1000

        sleep_calls: list[float] = []

        async def fake_show_reconnecting() -> None:
            pass

        await _run_gateway_session(
            ws_url="ws://host/ws/t",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=lambda: True,
            show_reconnecting=fake_show_reconnecting,
            max_reconnects=5,
            reconnect_delay=0.0,
            max_redirects=5,
        )

        assert len(calls) == 1
        assert not sleep_calls

    async def test_transient_close_reconnects_with_sleep(self) -> None:
        """close_code None/1006 → reconnect (with sleep) until max."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            return None  # transient

        async def show_reconnecting() -> None:
            pass

        await _run_gateway_session(
            ws_url="ws://host/ws/t",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=lambda: True,
            show_reconnecting=show_reconnecting,
            max_reconnects=2,
            reconnect_delay=0.0,
            max_redirects=5,
        )

        assert len(calls) == 3  # initial + 2 reconnects

    async def test_client_disconnect_stops_loop(self) -> None:
        """client_connected() returns False → stop immediately."""
        redirect_holder: list[str | None] = [None]
        calls: list[str] = []

        async def pump(url: str) -> int | None:
            calls.append(url)
            return None

        connected = [True, False]
        idx = 0

        def client_connected() -> bool:
            nonlocal idx
            val = connected[min(idx, len(connected) - 1)]
            idx += 1
            return val

        async def show_reconnecting() -> None:
            pass

        await _run_gateway_session(
            ws_url="ws://host/ws/t",
            redirect_holder=redirect_holder,
            pump=pump,
            client_connected=client_connected,
            show_reconnecting=show_reconnecting,
            max_reconnects=5,
            reconnect_delay=0.0,
            max_redirects=5,
        )

        # Connected on first iteration, pump runs, then disconnected → stop
        assert len(calls) == 1
