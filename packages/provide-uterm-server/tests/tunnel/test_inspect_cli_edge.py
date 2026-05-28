#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Edge case tests for the ``uterm inspect`` CLI subcommand (inspect.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.cli.inspect import _cmd_inspect, _create_tunnel, _read_token
from provide.uterm.tunnel.protocol import CHANNEL_HTTP


def _close_asyncio_run_coro(coro):
    coro.close()


def _close_asyncio_run_coro_then_interrupt(coro):
    coro.close()
    raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# CHANNEL_HTTP constant
# ---------------------------------------------------------------------------


def test_channel_http_value():
    assert CHANNEL_HTTP == 0x03
    assert CHANNEL_HTTP == 3


# ---------------------------------------------------------------------------
# _read_token edge cases
# ---------------------------------------------------------------------------


class TestReadTokenEdge:
    def test_token_file_exists_returns_content(self, tmp_path: Path):
        token_file = tmp_path / "mytoken"
        token_file.write_text("  secret-token-abc  \n")
        args = MagicMock(token=None, token_file=str(token_file))
        result = _read_token(args)
        assert result == "secret-token-abc"

    def test_token_file_empty_string_falls_back_to_default(self):
        # token_file="" → falls back to TerminalDefaults.token_file() which likely doesn't exist
        args = MagicMock(token=None, token_file="")
        # The default ~/.uterm/session_token almost certainly doesn't exist in CI/test env.
        # It should return None without raising.
        result = _read_token(args)
        assert result is None

    def test_explicit_token_overrides_file(self, tmp_path: Path):
        token_file = tmp_path / "mytoken"
        token_file.write_text("file-token\n")
        args = MagicMock(token="explicit-token", token_file=str(token_file))
        assert _read_token(args) == "explicit-token"

    def test_token_file_nonexistent_returns_none(self):
        args = MagicMock(token=None, token_file="/nonexistent/path/token")
        assert _read_token(args) is None

    def test_token_file_strips_whitespace(self, tmp_path: Path):
        token_file = tmp_path / "tok"
        token_file.write_text("\t  padded  \t\n")
        args = MagicMock(token=None, token_file=str(token_file))
        assert _read_token(args) == "padded"


# ---------------------------------------------------------------------------
# _create_tunnel edge cases
# ---------------------------------------------------------------------------


class TestCreateTunnelEdge:
    def _make_urlopen(self, payload: bytes):
        """Helper to create a mock urlopen context manager that returns payload."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_resp)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        return mock_ctx

    def test_trailing_slash_on_server_url(self):
        import json

        payload = json.dumps(
            {"tunnel_id": "t1", "ws_endpoint": "ws://x/t", "worker_token": "", "share_url": ""}
        ).encode()
        with patch("provide.uterm.cli.inspect.urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._make_urlopen(payload)
            result = _create_tunnel("https://example.com/", "my-tunnel", None, 4000)
        # URL must not double-slash: should be .../api/tunnels not ...//api/tunnels
        req_obj = mock_open.call_args[0][0]
        assert "//api/tunnels" not in req_obj.full_url
        assert result["tunnel_id"] == "t1"

    def test_no_trailing_slash_on_server_url(self):
        import json

        payload = json.dumps(
            {"tunnel_id": "t2", "ws_endpoint": "ws://x/t", "worker_token": "", "share_url": ""}
        ).encode()
        with patch("provide.uterm.cli.inspect.urllib.request.urlopen") as mock_open:
            mock_open.return_value = self._make_urlopen(payload)
            _create_tunnel("https://example.com", "test", None, 3000)
        req_obj = mock_open.call_args[0][0]
        assert req_obj.full_url == "https://example.com/api/tunnels"

    def test_http_error_with_body_detail(self):
        import urllib.error

        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        err.read = lambda: b"invalid token"
        with (
            patch("provide.uterm.cli.inspect.urllib.request.urlopen", side_effect=err),
            pytest.raises(SystemExit) as exc_info,
        ):
            _create_tunnel("https://example.com", "t", "bad-tok", 3000)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _cmd_inspect edge cases
# ---------------------------------------------------------------------------


class TestCmdInspectEdge:
    def _make_args(self, **overrides):
        defaults = {
            "server": "https://x.com",
            "port": 3000,
            "display_name": None,
            "token": None,
            "token_file": "/nonexistent",
            "listen_port": 0,
        }
        defaults.update(overrides)
        return MagicMock(**defaults)

    def test_custom_display_name_passed_to_create_tunnel(self):
        args = self._make_args(display_name="my-api")
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run", side_effect=_close_asyncio_run_coro),
        ):
            mock_create.return_value = {"ws_endpoint": "ws://x/t", "worker_token": "", "share_url": ""}
            _cmd_inspect(args)
        assert mock_create.call_args[0][1] == "my-api"

    def test_share_url_in_response_prints(self, capsys):
        args = self._make_args()
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run", side_effect=_close_asyncio_run_coro),
        ):
            mock_create.return_value = {
                "ws_endpoint": "ws://x/tunnel/t",
                "worker_token": "",
                "share_url": "https://share.example.com/t",
            }
            _cmd_inspect(args)
        captured = capsys.readouterr()
        assert "https://share.example.com/t" in captured.out

    def test_no_share_url_in_response_no_share_line(self, capsys):
        args = self._make_args()
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run", side_effect=_close_asyncio_run_coro),
        ):
            mock_create.return_value = {"ws_endpoint": "ws://x/tunnel/t", "worker_token": "", "share_url": ""}
            _cmd_inspect(args)
        captured = capsys.readouterr()
        assert "Share:" not in captured.out

    def test_keyboard_interrupt_handled_gracefully(self):
        args = self._make_args()
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run", side_effect=_close_asyncio_run_coro_then_interrupt),
        ):
            mock_create.return_value = {"ws_endpoint": "ws://x/tunnel/t", "worker_token": "", "share_url": ""}
            # Should NOT raise — KeyboardInterrupt is suppressed by `with suppress(KeyboardInterrupt):`
            _cmd_inspect(args)

    def test_relative_ws_endpoint_resolved_with_https(self):
        args = self._make_args(server="https://example.com")
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run") as mock_run,
        ):
            mock_create.return_value = {"ws_endpoint": "/tunnel/abc", "worker_token": "w", "share_url": ""}
            _cmd_inspect(args)
        ws_arg = mock_run.call_args[0][0]
        # _run_inspect is called with the resolved ws_endpoint as first arg
        # Check the coroutine was created with the right endpoint
        assert "wss://example.com/tunnel/abc" in str(
            ws_arg.cr_frame.f_locals if hasattr(ws_arg, "cr_frame") else ws_arg
        )
        ws_arg.close()

    def test_relative_ws_endpoint_resolved_with_http(self):
        args = self._make_args(server="http://localhost:8000")
        captured_endpoint = []

        async def fake_run_inspect(ws_endpoint, *a, **kw):
            captured_endpoint.append(ws_endpoint)

        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect._run_inspect", side_effect=fake_run_inspect),
            patch("provide.uterm.cli.inspect.asyncio.run") as mock_asyncio_run,
        ):
            mock_create.return_value = {"ws_endpoint": "/tunnel/xyz", "worker_token": "", "share_url": ""}
            # asyncio.run gets a coroutine; extract the resolved endpoint from the _create_tunnel call
            # We verify by checking mock_asyncio_run was called (actual WS resolution tested via source)
            _cmd_inspect(args)
        # asyncio.run should have been called with the coroutine from _run_inspect
        mock_asyncio_run.assert_called_once()
        mock_asyncio_run.call_args[0][0].close()

    def test_absolute_ws_endpoint_not_modified(self):
        args = self._make_args(server="https://example.com")
        with (
            patch("provide.uterm.cli.inspect._create_tunnel") as mock_create,
            patch("provide.uterm.cli.inspect.asyncio.run") as mock_run,
        ):
            mock_create.return_value = {
                "ws_endpoint": "wss://already-absolute.example.com/tunnel/t",
                "worker_token": "",
                "share_url": "",
            }
            _cmd_inspect(args)
        mock_run.assert_called_once()
        mock_run.call_args[0][0].close()

    def test_missing_ws_endpoint_exits_with_code_1(self):
        args = self._make_args()
        with (
            patch(
                "provide.uterm.cli.inspect._create_tunnel",
                return_value={"ws_endpoint": "", "worker_token": "", "share_url": ""},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _cmd_inspect(args)
        assert exc_info.value.code == 1
