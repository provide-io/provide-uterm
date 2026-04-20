#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for uterm inspect CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from provide.terminal.cli import _build_parser
from provide.terminal.cli.inspect import _cmd_inspect, _create_tunnel, _read_token


class TestInspectArgParsing:
    def test_inspect_subcommand_recognised(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://example.com"])
        assert args.port == 3000
        assert args.server == "https://example.com"

    def test_inspect_requires_port(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "-s", "https://example.com"])

    def test_inspect_requires_server(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "3000"])

    def test_inspect_has_func(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://x.com"])
        assert hasattr(args, "func")

    def test_inspect_listen_port(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://x.com", "--listen-port", "9123"])
        assert args.listen_port == 9123

    def test_inspect_display_name(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://x.com", "--display-name", "my-api"])
        assert args.display_name == "my-api"


class TestCreateTunnel:
    def test_success(self):
        resp = json.dumps({"tunnel_id": "t", "ws_endpoint": "ws://x", "worker_token": "w", "share_url": ""}).encode()
        with patch("provide.terminal.cli.inspect.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: resp)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            result = _create_tunnel("https://example.com", "test", None, 3000)
        assert result["tunnel_id"] == "t"

    def test_with_token(self):
        resp = json.dumps({"tunnel_id": "t", "ws_endpoint": "ws://x", "worker_token": "w", "share_url": ""}).encode()
        with patch("provide.terminal.cli.inspect.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: resp)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            _create_tunnel("https://example.com", "test", "my-token", 3000)
        req_obj = mock_open.call_args[0][0]
        assert req_obj.get_header("Authorization") == "Bearer my-token"

    def test_http_error(self):
        import urllib.error

        with patch("provide.terminal.cli.inspect.urllib.request.urlopen") as mock_open, pytest.raises(SystemExit):
            mock_open.side_effect = urllib.error.HTTPError("http://x", 500, "fail", {}, None)
            _create_tunnel("https://example.com", "test", None, 3000)

    def test_url_error(self):
        import urllib.error

        with patch("provide.terminal.cli.inspect.urllib.request.urlopen") as mock_open, pytest.raises(SystemExit):
            mock_open.side_effect = urllib.error.URLError("no host")
            _create_tunnel("https://example.com", "test", None, 3000)


class TestReadToken:
    def test_explicit_token(self):
        args = MagicMock(token="my-tok", token_file=None)
        assert _read_token(args) == "my-tok"

    def test_no_token(self):
        args = MagicMock(token=None, token_file="/nonexistent")
        assert _read_token(args) is None


class TestCmdInspect:
    def test_missing_ws_endpoint(self):
        args = MagicMock(
            server="https://x.com", port=3000, display_name=None, token=None, token_file="/nonexistent", listen_port=0
        )
        with (
            patch(
                "provide.terminal.cli.inspect._create_tunnel",
                return_value={"ws_endpoint": "", "worker_token": "", "share_url": ""},
            ),
            pytest.raises(SystemExit),
        ):
            _cmd_inspect(args)

    def test_default_display_name(self):
        args = MagicMock(
            server="https://x.com", port=3000, display_name=None, token=None, token_file="/nonexistent", listen_port=0
        )
        with (
            patch("provide.terminal.cli.inspect._create_tunnel") as mock_create,
            patch("provide.terminal.cli.inspect.asyncio.run"),
        ):
            mock_create.return_value = {"ws_endpoint": "ws://x/tunnel/t", "worker_token": "", "share_url": ""}
            _cmd_inspect(args)
            assert mock_create.call_args[0][1] == "http:3000"

    def test_relative_ws_endpoint(self):
        args = MagicMock(
            server="https://example.com",
            port=3000,
            display_name="test",
            token=None,
            token_file="/nonexistent",
            listen_port=0,
        )
        with (
            patch(
                "provide.terminal.cli.inspect._create_tunnel",
                return_value={"ws_endpoint": "/tunnel/t", "worker_token": "w", "share_url": ""},
            ),
            patch("provide.terminal.cli.inspect.asyncio.run") as mock_run,
        ):
            _cmd_inspect(args)
            mock_run.assert_called_once()
