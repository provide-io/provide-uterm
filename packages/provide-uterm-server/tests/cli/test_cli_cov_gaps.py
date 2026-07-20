#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pin residual CLI / watch pure-path branches for expanded server cov scope."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from provide.uterm.cli import _cmd_proxy, _run_listen
from provide.uterm.cli._watch_app import parse_http_frames
from provide.uterm.cli.watch import extract_tunnel_id


def test_extract_tunnel_id_url_without_match_returns_full_value() -> None:
    """URL with :// but no tunnel path falls through to return value (28->30)."""
    raw = "https://example.com/docs/other"
    assert extract_tunnel_id(raw) == raw


def test_extract_tunnel_id_url_with_session_path() -> None:
    assert extract_tunnel_id("https://host/app/session/abc-123?x=1") == "abc-123"


def test_parse_http_frames_skips_non_http_and_bad_json() -> None:
    # malformed length + non-http channel should not raise
    raw = '\x10\x02deadbeef:not-json\x10\x020000000a:{"x":1}'
    assert parse_http_frames(raw) == []


def test_cmd_proxy_ssh_transport_missing_attr_exits() -> None:
    """AttributeError when SSHTransport is None (cli/__init__.py:83-84)."""
    args = argparse.Namespace(
        transport="ssh",
        host="127.0.0.1",
        bbs_port=23,
        port=0,
        path="/ws",
        cols=80,
        rows=24,
        title="t",
        no_ui=True,
    )
    mod = ModuleType("provide.uterm.transports.ssh")
    # SSHTransport absent → getattr None → AttributeError
    with (
        patch.dict(sys.modules, {"provide.uterm.transports.ssh": mod}),
        patch("importlib.import_module", return_value=mod),
        pytest.raises(SystemExit) as exc,
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        _cmd_proxy(args)
    assert exc.value.code == 1
    assert "asyncssh" in err.getvalue() or "SSH" in err.getvalue()


@pytest.mark.asyncio
async def test_run_listen_authorized_keys_suffix_optional_and_required() -> None:
    """authorized_keys path constructs resolver + prints pubkey suffix (227-242)."""
    started: list[object] = []

    class _Srv:
        async def serve_forever(self) -> None:
            await asyncio.Event().wait()

        def close(self) -> None:
            return

    class _FakeSsh:
        def __init__(self, *a: object, **kw: object) -> None:
            self.kw = kw
            started.append(self)

        async def start(self, bind: str, port: int) -> _Srv:
            return _Srv()

    class _FakeTelnet:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def start(self, bind: str, port: int) -> _Srv:
            return _Srv()

    with (
        patch("provide.uterm.auth.AuthorizedKeysFileResolver") as res_cls,
        contextlib.redirect_stdout(io.StringIO()) as out,
    ):
        res_cls.return_value = object()
        task = asyncio.create_task(
            _run_listen(
                "ws://127.0.0.1/ws",
                "127.0.0.1",
                0,
                2222,
                None,
                "passthrough",
                _FakeTelnet,
                _FakeSsh,
                authorized_keys="/tmp/keys",
                require_resolver=False,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert "pubkey" in out.getvalue()
        assert started and started[0].kw.get("key_resolver") is not None
        assert started[0].kw.get("require_resolver") is False

    started.clear()
    with (
        patch("provide.uterm.auth.AuthorizedKeysFileResolver") as res_cls,
        contextlib.redirect_stdout(io.StringIO()) as out2,
    ):
        res_cls.return_value = object()
        task2 = asyncio.create_task(
            _run_listen(
                "ws://127.0.0.1/ws",
                "127.0.0.1",
                0,
                2223,
                None,
                "passthrough",
                _FakeTelnet,
                _FakeSsh,
                authorized_keys="/tmp/keys",
                require_resolver=True,
            )
        )
        await asyncio.sleep(0.05)
        task2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task2
        assert "required" in out2.getvalue()
        assert started[0].kw.get("require_resolver") is True


def test_watch_app_handle_http_res_unknown_id() -> None:
    """http_res for unknown id takes 245->exit without update."""
    from provide.uterm.cli._watch_app import Exchange, WatchApp

    app = object.__new__(WatchApp)
    app._exchanges = [Exchange(req_id="a", method="GET", url="/")]
    app._request_count = 1
    app._connected = True
    app._method_filter = None
    app._handle_frame(
        {
            "type": "http_res",
            "id": "missing",
            "status": 200,
            "status_text": "OK",
            "duration_ms": 1,
            "headers": {},
            "body_b64": None,
            "body_size": 0,
            "body_truncated": False,
            "body_binary": False,
        }
    )
    assert app._exchanges[0].status is None


def test_cmd_inspect_prints_without_intercept_flag() -> None:
    """_cmd_inspect intercept=False skips intercept print (123->125)."""
    from provide.uterm.cli.inspect import _cmd_inspect

    args = SimpleNamespace(
        server="http://127.0.0.1:9",
        display_name="d",
        port=18080,
        listen_port=0,
        intercept=False,
        intercept_timeout=30,
        intercept_timeout_action="forward",
        token=None,
        token_file="/nonexistent",
    )
    tunnel = {
        "ws_endpoint": "/ws/worker/x/term",
        "worker_token": "tok",
        "share_url": "",
    }
    with (
        patch("provide.uterm.cli.inspect._create_tunnel", return_value=tunnel),
        patch("asyncio.run") as arun,
        contextlib.redirect_stdout(io.StringIO()) as out,
    ):
        arun.side_effect = KeyboardInterrupt
        with contextlib.suppress(KeyboardInterrupt):
            _cmd_inspect(args)
    text = out.getvalue()
    assert "Inspecting HTTP" in text
    assert "Intercept: ON" not in text
