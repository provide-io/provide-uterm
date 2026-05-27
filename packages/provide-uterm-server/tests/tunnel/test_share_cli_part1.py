#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for ``uterm share`` CLI subcommand."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.cli import _build_parser
from provide.uterm.cli.share import (
    _cmd_share,
    _create_tunnel,
    _display_name,
    _read_token,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TUNNEL_RESPONSE: dict[str, Any] = {
    "tunnel_id": "tun-abc123",
    "share_url": "https://warp.example.com/view/tun-abc123",
    "control_url": "https://warp.example.com/control/tun-abc123",
    "ws_endpoint": "wss://warp.example.com/ws/tunnel/tun-abc123",
    "worker_token": "tok-worker-secret",
}


def _make_args(**overrides: Any) -> Any:
    """Build a minimal argparse.Namespace for _cmd_share."""
    defaults: dict[str, Any] = {
        "server": "https://warp.example.com",
        "cmd": None,
        "token": None,
        "token_file": "/nonexistent/.uterm/session_token",
        "attach": False,
        "display_name": None,
    }
    defaults.update(overrides)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestShareArgParsing:
    def test_share_subcommand_recognised(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["share", "--server", "https://x.com"])
        assert args.command == "share"
        assert args.server == "https://x.com"

    def test_share_with_command(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["share", "--server", "https://x.com", "htop"])
        assert args.cmd == ["htop"]

    def test_share_with_multi_word_command(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["share", "-s", "https://x.com", "--", "bash", "-c", "echo hi"])
        assert args.cmd == ["bash", "-c", "echo hi"]

    def test_share_all_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "share",
                "--server",
                "https://x.com",
                "--token",
                "my-token",
                "--token-file",
                "/path/to/tok",
                "--attach",
                "--display-name",
                "me@box",
            ]
        )
        assert args.token == "my-token"
        assert args.token_file == "/path/to/tok"
        assert args.attach is True
        assert args.display_name == "me@box"

    def test_share_requires_server(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["share"])

    def test_share_has_func(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["share", "-s", "https://x.com"])
        assert args.func is _cmd_share


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


class TestReadToken:
    def test_explicit_token(self) -> None:
        args = _make_args(token="explicit-tok")
        assert _read_token(args) == "explicit-tok"

    def test_token_from_file(self, tmp_path: Path) -> None:
        tok_file = tmp_path / "tok"
        tok_file.write_text("file-tok\n")
        args = _make_args(token=None, token_file=str(tok_file))
        assert _read_token(args) == "file-tok"

    def test_no_token(self) -> None:
        args = _make_args(token=None, token_file="/does/not/exist")
        assert _read_token(args) is None


# ---------------------------------------------------------------------------
# Display name
# ---------------------------------------------------------------------------


class TestDisplayName:
    def test_explicit_display_name(self) -> None:
        args = _make_args(display_name="custom@name")
        assert _display_name(args) == "custom@name"

    def test_auto_detect(self) -> None:
        args = _make_args(display_name=None)
        name = _display_name(args)
        assert "@" in name
        assert len(name) > 2


# ---------------------------------------------------------------------------
# _create_tunnel
# ---------------------------------------------------------------------------


class TestCreateTunnel:
    def test_success(self) -> None:
        resp_body = json.dumps(_TUNNEL_RESPONSE).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("provide.uterm.cli.share.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = _create_tunnel("https://warp.example.com", "me@box", "tok-123")

        assert result == _TUNNEL_RESPONSE
        call_req = mock_open.call_args[0][0]
        assert call_req.full_url == "https://warp.example.com/api/tunnels"
        assert call_req.get_header("Authorization") == "Bearer tok-123"
        assert call_req.get_header("Content-type") == "application/json"

    def test_success_no_token(self) -> None:
        resp_body = json.dumps(_TUNNEL_RESPONSE).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("provide.uterm.cli.share.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            _create_tunnel("https://warp.example.com/", "me@box", None)

        call_req = mock_open.call_args[0][0]
        assert call_req.full_url == "https://warp.example.com/api/tunnels"
        assert not call_req.has_header("Authorization")

    def test_http_error(self) -> None:
        exc = urllib.error.HTTPError(
            "https://x.com/api/tunnels",
            401,
            "Unauthorized",
            {},
            BytesIO(b"bad token"),
        )
        with (
            patch("provide.uterm.cli.share.urllib.request.urlopen", side_effect=exc),
            pytest.raises(SystemExit),
        ):
            _create_tunnel("https://x.com", "me@box", "bad")

    def test_url_error(self) -> None:
        exc = urllib.error.URLError("Connection refused")
        with (
            patch("provide.uterm.cli.share.urllib.request.urlopen", side_effect=exc),
            pytest.raises(SystemExit),
        ):
            _create_tunnel("https://x.com", "me@box", None)

    def test_trailing_slash_stripped(self) -> None:
        resp_body = json.dumps(_TUNNEL_RESPONSE).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("provide.uterm.cli.share.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            _create_tunnel("https://warp.example.com///", "me@box", None)

        call_req = mock_open.call_args[0][0]
        assert call_req.full_url == "https://warp.example.com/api/tunnels"


# ---------------------------------------------------------------------------
# _cmd_share — full flow with mocks
# ---------------------------------------------------------------------------


class TestCmdShare:
    def _mock_create_tunnel(self, resp: dict[str, Any] | None = None) -> MagicMock:
        return patch(
            "provide.uterm.cli.share._create_tunnel",
            return_value=resp or _TUNNEL_RESPONSE,
        )

    def test_spawn_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Happy path: spawn PTY, connect WS, print URLs."""
        mock_pty = MagicMock()
        mock_pty.close = MagicMock()

        with (
            self._mock_create_tunnel(),
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty) as mock_spawn,
            patch("provide.uterm.cli.share.asyncio.run") as mock_run,
            patch("provide.uterm.cli.share._read_token", return_value="tok"),
        ):
            args = _make_args(cmd=["bash"])
            _cmd_share(args)

        mock_spawn.assert_called_once_with(["bash"])
        mock_run.assert_called_once()
        mock_pty.close.assert_called_once()

        out = capsys.readouterr().out
        assert "Sharing terminal session..." in out
        assert _TUNNEL_RESPONSE["share_url"] in out
        assert _TUNNEL_RESPONSE["control_url"] in out
        assert "Ctrl+C" in out

    def test_attach_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--attach uses TtyProxy instead of spawn_pty."""
        mock_tty = MagicMock()
        mock_tty.start.return_value = (80, 24)
        mock_tty.close = MagicMock()

        with (
            self._mock_create_tunnel(),
            patch("provide.uterm.cli.share.TtyProxy", return_value=mock_tty) as mock_cls,
            patch("provide.uterm.cli.share.asyncio.run"),
            patch("provide.uterm.cli.share._read_token", return_value=None),
        ):
            args = _make_args(attach=True)
            _cmd_share(args)

        mock_cls.assert_called_once()
        mock_tty.start.assert_called_once()
        mock_tty.close.assert_called_once()

    def test_default_cmd_is_none(self) -> None:
        """When cmd is empty list, passes None to spawn_pty (uses $SHELL)."""
        mock_pty = MagicMock()

        with (
            self._mock_create_tunnel(),
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty) as mock_spawn,
            patch("provide.uterm.cli.share.asyncio.run"),
            patch("provide.uterm.cli.share._read_token", return_value=None),
        ):
            args = _make_args(cmd=[])
            _cmd_share(args)

        mock_spawn.assert_called_once_with(None)

    def test_keyboard_interrupt_clean_shutdown(self) -> None:
        """Ctrl+C during bridge loop → PTY closed cleanly."""
        mock_pty = MagicMock()

        with (
            self._mock_create_tunnel(),
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty),
            patch("provide.uterm.cli.share.asyncio.run", side_effect=KeyboardInterrupt),
            patch("provide.uterm.cli.share._read_token", return_value=None),
        ):
            args = _make_args()
            _cmd_share(args)  # should not raise

        mock_pty.close.assert_called_once()

    def test_missing_ws_endpoint(self) -> None:
        """Server response without ws_endpoint → sys.exit."""
        bad_resp = {**_TUNNEL_RESPONSE, "ws_endpoint": ""}

        with (
            patch("provide.uterm.cli.share._create_tunnel", return_value=bad_resp),
            patch("provide.uterm.cli.share._read_token", return_value=None),
            pytest.raises(SystemExit),
        ):
            args = _make_args()
            _cmd_share(args)

    def test_display_name_passed_to_create(self) -> None:
        """--display-name is forwarded to _create_tunnel."""
        mock_pty = MagicMock()

        with (
            patch("provide.uterm.cli.share._create_tunnel", return_value=_TUNNEL_RESPONSE) as mock_ct,
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty),
            patch("provide.uterm.cli.share.asyncio.run"),
            patch("provide.uterm.cli.share._read_token", return_value="t"),
        ):
            args = _make_args(display_name="custom@host")
            _cmd_share(args)

        mock_ct.assert_called_once_with("https://warp.example.com", "custom@host", "t")


# ---------------------------------------------------------------------------
# _run_share — async WebSocket bridge
# ---------------------------------------------------------------------------
