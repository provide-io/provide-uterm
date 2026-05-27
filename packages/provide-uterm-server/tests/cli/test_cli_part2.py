#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.cli (uterm entry point)."""

from __future__ import annotations

import asyncio
import contextlib
import io

import pytest

pytestmark = pytest.mark.timeout(5)

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestRunListen:
    async def test_run_listen_telnet_only(self) -> None:
        """_run_listen with telnet_port > 0 starts a server and stops on cancel."""
        from provide.uterm.cli import _run_listen

        start_calls: list[tuple[str, int]] = []

        class _FakeServer:
            closed = False

            async def serve_forever(self) -> None:
                await asyncio.sleep(100)

            def close(self) -> None:
                self.closed = True

        class _FakeGateway:
            def __init__(self, ws_url: str, **_: object) -> None:
                pass

            async def start(self, host: str, port: int) -> _FakeServer:
                start_calls.append((host, port))
                return _FakeServer()

        class _FakeSshGateway:
            pass

        task = asyncio.create_task(
            _run_listen("ws://localhost/ws", "127.0.0.1", 2112, 0, None, "passthrough", _FakeGateway, _FakeSshGateway)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert start_calls == [("127.0.0.1", 2112)]

    async def test_run_listen_no_ports_is_noop(self) -> None:
        """_run_listen with both ports=0 starts nothing and returns."""
        from provide.uterm.cli import _run_listen

        class _FakeGateway:
            async def start(self, host: str, port: int) -> object:
                raise AssertionError("Should not be called")

        await _run_listen("ws://localhost/ws", "127.0.0.1", 0, 0, None, "passthrough", _FakeGateway, _FakeGateway)

    async def test_run_listen_ssh_port_starts_ssh(self) -> None:
        """_run_listen with ssh_port > 0 starts an SSH gateway."""
        from provide.uterm.cli import _run_listen

        class _FakeTelnetGateway:
            pass

        ssh_started = []

        class _FakeSshServer:
            async def serve_forever(self) -> None:
                await asyncio.sleep(100)

            def close(self) -> None:
                pass

        class _FakeSshGateway:
            def __init__(self, ws_url: str, **_: object) -> None:
                pass

            async def start(self, host: str, port: int) -> _FakeSshServer:
                ssh_started.append(port)
                return _FakeSshServer()

        task = asyncio.create_task(
            _run_listen(
                "ws://localhost/ws",
                "127.0.0.1",
                0,
                12345,
                None,
                "passthrough",
                _FakeTelnetGateway,
                _FakeSshGateway,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ssh_started == [12345]

    async def test_run_listen_ssh_import_error_warns(self) -> None:
        """_run_listen continues when SshWsGateway raises ImportError."""
        from provide.uterm.cli import _run_listen

        class _BadSshGateway:
            def __init__(self, ws_url: str, **kw: object) -> None:
                raise ImportError("asyncssh missing")

        class _FakeTelnetGatewayWithStart:
            def __init__(self, ws_url: str, **_: object) -> None:
                pass

            async def start(self, host: str, port: int) -> object:
                class S:
                    async def serve_forever(self) -> None:
                        await asyncio.sleep(100)

                    def close(self) -> None:
                        pass

                return S()

        task = asyncio.create_task(
            _run_listen(
                "ws://localhost/ws",
                "127.0.0.1",
                2112,
                2222,
                None,
                "passthrough",
                _FakeTelnetGatewayWithStart,
                _BadSshGateway,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_cmd_listen_both_ports_zero_exits(self) -> None:
        """_cmd_listen exits with code 1 when both --port and --ssh-port are 0."""
        from provide.uterm.cli import _build_parser, _cmd_listen

        args = _build_parser().parse_args(["listen", "ws://localhost", "--port", "0", "--ssh-port", "0"])
        captured = io.StringIO()
        with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stderr(captured):
            _cmd_listen(args)
        assert exc_info.value.code == 1
        assert "non-zero" in captured.getvalue()
