#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.cli (uterm entry point)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.cli import _build_parser, main

pytestmark = pytest.mark.timeout(5)

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParser:
    def test_proxy_minimal(self) -> None:
        args = _build_parser().parse_args(["proxy", "bbs.example.com", "23"])
        assert args.command == "proxy"
        assert args.host == "bbs.example.com"
        assert args.bbs_port == 23
        assert args.port == 8765
        assert args.bind == "0.0.0.0"
        assert args.path == "/ws/terminal"
        assert args.transport == "telnet"

    def test_proxy_all_options(self) -> None:
        args = _build_parser().parse_args(
            [
                "proxy",
                "bbs.example.com",
                "23",
                "--port",
                "9000",
                "--bind",
                "127.0.0.1",
                "--path",
                "/ws/bbs",
                "--transport",
                "ssh",
            ]
        )
        assert args.command == "proxy"
        assert args.port == 9000
        assert args.bind == "127.0.0.1"
        assert args.path == "/ws/bbs"
        assert args.transport == "ssh"

    def test_proxy_short_port_flag(self) -> None:
        args = _build_parser().parse_args(["proxy", "host", "23", "-p", "1234"])
        assert args.port == 1234

    def test_no_subcommand_exits(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args([])

    def test_invalid_transport_exits(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["proxy", "host", "23", "--transport", "ftp"])

    def test_bbs_port_must_be_int(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["proxy", "host", "notanint"])


# ---------------------------------------------------------------------------
# _cmd_proxy tests (mock uvicorn so we don't actually start a server)
# ---------------------------------------------------------------------------


class TestCmdProxy:
    def _make_args(self, **overrides):
        args = _build_parser().parse_args(["proxy", "bbs.example.com", "23"])
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def test_proxy_calls_uvicorn_run(self) -> None:
        mock_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
            main(["proxy", "bbs.example.com", "23"])

        mock_uvicorn.run.assert_called_once()
        call_kwargs = mock_uvicorn.run.call_args
        assert call_kwargs.kwargs["host"] == "0.0.0.0"
        assert call_kwargs.kwargs["port"] == 8765

    def test_proxy_custom_port_and_bind(self) -> None:
        mock_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
            main(["proxy", "bbs.example.com", "23", "--port", "9000", "--bind", "127.0.0.1"])

        call_kwargs = mock_uvicorn.run.call_args
        assert call_kwargs.kwargs["host"] == "127.0.0.1"
        assert call_kwargs.kwargs["port"] == 9000

    def test_proxy_app_has_ws_route(self) -> None:
        """The FastAPI app passed to uvicorn includes the WS router."""
        captured_app = {}
        mock_uvicorn = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = mock_uvicorn

        with patch.dict("sys.modules", {"uvicorn": mock_uv_mod}):
            main(["proxy", "bbs.example.com", "23", "--path", "/ws/bbs"])

        app = captured_app["app"]
        routes = {r.path for r in app.routes}
        assert "/ws/bbs" in routes

    def test_proxy_app_has_terminal_page(self) -> None:
        """The proxy app serves a terminal HTML page at /."""
        from starlette.testclient import TestClient

        captured_app = {}
        mock_uvicorn = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = mock_uvicorn

        with patch.dict("sys.modules", {"uvicorn": mock_uv_mod}):
            main(["proxy", "bbs.example.com", "23"])

        client = TestClient(captured_app["app"])
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "bbs.example.com:23" in resp.text
        assert "/ws/terminal" in resp.text
        assert "ProvideTerminal" in resp.text

    def test_proxy_app_serves_static_frontend(self) -> None:
        """The proxy app mounts the frontend directory at /static."""
        captured_app = {}
        mock_uvicorn = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = mock_uvicorn

        with patch.dict("sys.modules", {"uvicorn": mock_uv_mod}):
            main(["proxy", "bbs.example.com", "23"])

        app = captured_app["app"]
        route_names = {getattr(r, "name", None) for r in app.routes}
        assert "frontend" in route_names

    def test_proxy_no_static_mount_when_frontend_missing(self) -> None:
        """When frontend directory doesn't exist, static mount is skipped."""
        captured_app = {}
        mock_uvicorn = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = mock_uvicorn

        with (
            patch.dict("sys.modules", {"uvicorn": mock_uv_mod}),
            patch("provide.uterm.cli._FRONTEND_DIR", MagicMock(is_dir=MagicMock(return_value=False))),
        ):
            main(["proxy", "bbs.example.com", "23"])

        app = captured_app["app"]
        route_names = {getattr(r, "name", None) for r in app.routes}
        assert "frontend" not in route_names

    def test_missing_uvicorn_exits(self) -> None:
        """SystemExit(1) when uvicorn is not installed."""
        # Drop any cached importers of uvicorn so the missing-module signal
        # actually reaches ``_cmd_proxy``'s try/except instead of being
        # short-circuited by a previously-loaded sibling module that already
        # bound the name. Without this the test passes or fails depending
        # on collection order.
        original = sys.modules.get("uvicorn")
        cached_server_cli = sys.modules.pop("provide.uterm.server.cli", None)
        sys.modules["uvicorn"] = None  # type: ignore[assignment]
        try:
            captured = io.StringIO()
            with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stderr(captured):
                main(["proxy", "bbs.example.com", "23"])
            assert exc_info.value.code == 1
            assert "missing dependency" in captured.getvalue()
        finally:
            if original is None:
                sys.modules.pop("uvicorn", None)
            else:
                sys.modules["uvicorn"] = original
            if cached_server_cli is not None:
                sys.modules["provide.uterm.server.cli"] = cached_server_cli

    def test_proxy_passes_transport_factory(self) -> None:
        """_cmd_proxy passes a non-None transport_factory to WsTerminalProxy."""

        captured_factory = []

        class _CapturingProxy:
            def __init__(self, host, port, *, transport_factory=None):
                captured_factory.append(transport_factory)

            def create_router(self, path):
                from fastapi import APIRouter

                return APIRouter()

        mock_uvicorn = MagicMock()
        with (
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch("provide.uterm.fastapi_utils.WsTerminalProxy", _CapturingProxy),
        ):
            main(["proxy", "bbs.example.com", "23"])

        assert len(captured_factory) == 1
        assert captured_factory[0] is not None

    def test_ssh_transport_selected(self) -> None:
        """--transport ssh uses SSHTransport, or exits cleanly if asyncssh missing."""
        mock_uvicorn = MagicMock()
        mock_ssh_module = MagicMock()
        mock_ssh_module.SSHTransport = MagicMock()
        with (
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn, "provide.uterm.transports.ssh": mock_ssh_module}),
        ):
            # Just verify the SSH branch is exercised without raising unexpectedly
            try:
                main(["proxy", "bbs.example.com", "22", "--transport", "ssh"])
            except SystemExit as exc:
                assert exc.code == 1  # only acceptable exit is missing-dep


# ---------------------------------------------------------------------------
# listen subcommand parser tests
# ---------------------------------------------------------------------------


class TestListenParser:
    def test_listen_minimal(self) -> None:
        args = _build_parser().parse_args(["listen", "wss://warp.provide.io/ws/terminal"])
        assert args.command == "listen"
        assert args.ws_url == "wss://warp.provide.io/ws/terminal"
        assert args.port == 2112
        assert args.ssh_port == 0
        assert args.bind == "0.0.0.0"
        assert args.server_key is None

    def test_listen_all_options(self) -> None:
        args = _build_parser().parse_args(
            [
                "listen",
                "wss://example.com/ws",
                "--port",
                "2323",
                "--ssh-port",
                "2222",
                "--bind",
                "127.0.0.1",
                "--server-key",
                "/etc/host_key",
            ]
        )
        assert args.port == 2323
        assert args.ssh_port == 2222
        assert args.bind == "127.0.0.1"
        assert args.server_key == "/etc/host_key"

    def test_listen_short_port(self) -> None:
        args = _build_parser().parse_args(["listen", "ws://localhost/ws", "-p", "9999"])
        assert args.port == 9999

    def test_listen_disable_telnet(self) -> None:
        args = _build_parser().parse_args(["listen", "ws://localhost/ws", "--port", "0"])
        assert args.port == 0

    def test_listen_missing_url_exits(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["listen"])

    def test_listen_authorized_keys_defaults(self) -> None:
        """Without --authorized-keys, resolver is absent; require flag is False."""
        args = _build_parser().parse_args(["listen", "ws://x"])
        assert args.authorized_keys is None
        assert args.require_resolver is False

    def test_listen_authorized_keys_flag(self) -> None:
        args = _build_parser().parse_args(["listen", "ws://x", "--authorized-keys", "/etc/ssh_keys"])
        assert args.authorized_keys == "/etc/ssh_keys"
        assert args.require_resolver is False

    def test_listen_require_authorized_keys_flag(self) -> None:
        args = _build_parser().parse_args(
            [
                "listen",
                "ws://x",
                "--authorized-keys",
                "/etc/ssh_keys",
                "--require-authorized-keys",
            ]
        )
        assert args.authorized_keys == "/etc/ssh_keys"
        assert args.require_resolver is True

    def test_listen_require_without_file_errors(self) -> None:
        """--require-authorized-keys alone (no file) must fail fast."""
        # parse succeeds; the validation runs in _cmd_listen.
        args = _build_parser().parse_args(["listen", "ws://x", "--ssh-port", "2222", "--require-authorized-keys"])
        assert args.authorized_keys is None
        assert args.require_resolver is True

        captured = io.StringIO()
        with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stderr(captured):
            from provide.uterm.cli import _cmd_listen

            _cmd_listen(args)
        assert exc_info.value.code == 1
        assert "--require-authorized-keys" in captured.getvalue()


# ---------------------------------------------------------------------------
# _cmd_listen tests
# ---------------------------------------------------------------------------


class TestCmdListen:
    async def test_listen_starts_telnet_gateway(self) -> None:
        """_run_listen starts a TCP server and can be cancelled cleanly."""
        import websockets

        async def _handler(ws) -> None:
            await ws.send("hi")

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port = ws_srv.sockets[0].getsockname()[1]
        ws_url = f"ws://127.0.0.1:{ws_port}"
        try:
            from provide.uterm.cli import _run_listen
            from provide.uterm.gateway import SshWsGateway, TelnetWsGateway

            task = asyncio.create_task(
                _run_listen(ws_url, "127.0.0.1", 0, 0, None, "passthrough", TelnetWsGateway, SshWsGateway)
            )
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, SystemExit):
                await task
        finally:
            ws_srv.close()

    async def test_listen_e2e_telnet_client(self) -> None:
        """Full pipe: telnet client → TelnetWsGateway → WS echo server."""
        import websockets

        banner = b"gateway works!\r\n"

        async def _handler(ws) -> None:
            await ws.send(banner.decode("latin-1"))
            async for msg in ws:
                await ws.send(msg)

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port = ws_srv.sockets[0].getsockname()[1]
        try:
            from provide.uterm.gateway import TelnetWsGateway

            # iac_negotiate=False: this test asserts on byte-level banner
            # echo; the default TTYPE/NEW-ENVIRON handshake would otherwise
            # race the banner on a short read.
            gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}", iac_negotiate=False)
            tcp_srv = await gw.start("127.0.0.1", 0)
            from asyncio import Server

            assert isinstance(tcp_srv, Server)
            assert tcp_srv.sockets is not None
            tcp_port = tcp_srv.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            writer.close()
            tcp_srv.close()
        finally:
            ws_srv.close()

        assert b"gateway works!" in data


class TestCmdProxySshImportError:
    def test_ssh_import_error_exits(self) -> None:
        """SSH import failure in _cmd_proxy prints error to stderr and exits with code 1."""

        original = sys.modules.get("provide.uterm.transports.ssh")
        sys.modules["provide.uterm.transports.ssh"] = None  # type: ignore[assignment]
        try:
            captured = io.StringIO()
            with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stderr(captured):
                main(["proxy", "bbs.example.com", "23", "--transport", "ssh"])
            assert exc_info.value.code == 1
            assert "asyncssh" in captured.getvalue().lower() or "ssh" in captured.getvalue().lower()
        finally:
            if original is None:
                sys.modules.pop("provide.uterm.transports.ssh", None)
            else:
                sys.modules["provide.uterm.transports.ssh"] = original
