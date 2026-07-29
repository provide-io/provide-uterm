#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for `uterm inspect`.

Inspecting puts a proxy in front of a local port and shares what crosses it,
so what the command decides before anything is proxied matters:

* **Where the WebSocket is**, resolved from whatever the server answered —
  a full address taken as it stands, a path joined to the server it came from
  with the scheme upgraded, so a session shared over TLS is carried over TLS.
* **What the session is called**, which defaults to the port being inspected
  rather than to nothing.
* **What is printed**, because the share link is the whole output — and
  because a caller who has turned interception on needs to be told, since a
  proxy that pauses requests looks exactly like one that has hung.

The command is driven with the network and the proxy replaced by recorders.

# ruff: noqa: S106

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_inspect_golden.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any

from provide.uterm.cli import inspect as cli_inspect

OUT = Path(__file__).resolve().parent / "inspect_golden.json"

TUNNEL_INFO: dict[str, Any] = {
    "tunnel_id": "t-1",
    "share_url": "https://warp.example/app/inspect/t-1",
    "ws_endpoint": "/tunnel/abc",
    "worker_token": "worker-token",
}


def _namespace(**fields: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {"server": "https://warp.example", "port": 8080, "token": "t"}
    defaults.update(fields)
    return argparse.Namespace(**defaults)


def _run(name: str, args: argparse.Namespace, tunnel_info: dict[str, Any]) -> dict[str, Any]:
    """Drive the real command with the network and the proxy replaced."""
    ran: dict[str, Any] = {}

    def fake_create(server: str, display_name: str, token: str | None, target_port: int) -> dict[str, Any]:
        ran["created"] = {
            "server": server,
            "display_name": display_name,
            "token": token,
            "target_port": target_port,
        }
        return dict(tunnel_info)

    async def fake_run_inspect(
        ws_endpoint: str, worker_token: str, target_port: int, listen_port: int, **kw: Any
    ) -> None:
        ran["inspect"] = {
            "ws_endpoint": ws_endpoint,
            "worker_token": worker_token,
            "target_port": target_port,
            "listen_port": listen_port,
            **dict(kw),
        }

    real_create = cli_inspect._create_tunnel
    real_run = cli_inspect._run_inspect
    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code: int | None = None
    try:
        cli_inspect._create_tunnel = fake_create  # type: ignore[assignment]
        cli_inspect._run_inspect = fake_run_inspect  # type: ignore[assignment]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli_inspect._cmd_inspect(args)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
    finally:
        cli_inspect._create_tunnel = real_create  # type: ignore[assignment]
        cli_inspect._run_inspect = real_run  # type: ignore[assignment]

    return {
        "name": name,
        "args": dict(vars(args)),
        "tunnel_info": tunnel_info,
        "ran": ran,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "exit_code": exit_code,
    }


CASES: list[tuple[str, argparse.Namespace, dict[str, Any]]] = [
    ("an ordinary run", _namespace(), TUNNEL_INFO),
    ("a port to listen on", _namespace(listen_port=9000), TUNNEL_INFO),
    ("a name given", _namespace(display_name="my api"), TUNNEL_INFO),
    ("a name given as nothing", _namespace(display_name=""), TUNNEL_INFO),
    ("another port", _namespace(port=3000), TUNNEL_INFO),
    ("interception on", _namespace(intercept=True), TUNNEL_INFO),
    (
        "interception with its own timeout",
        _namespace(intercept=True, intercept_timeout=5.0, intercept_timeout_action="drop"),
        TUNNEL_INFO,
    ),
    ("an absolute endpoint", _namespace(), {**TUNNEL_INFO, "ws_endpoint": "wss://elsewhere.example/t"}),
    ("a server over cleartext", _namespace(server="http://warp.example"), TUNNEL_INFO),
    ("a server with a trailing slash", _namespace(server="https://warp.example/"), TUNNEL_INFO),
    ("an answer with no endpoint", _namespace(), {**TUNNEL_INFO, "ws_endpoint": ""}),
    ("an answer with nothing in it", _namespace(), {}),
    (
        "an answer naming a session rather than a tunnel",
        _namespace(),
        {**TUNNEL_INFO, "tunnel_id": "", "session_id": "s-9"},
    ),
    ("an answer with no share link", _namespace(), {**TUNNEL_INFO, "share_url": ""}),
]


def main() -> None:
    corpus = {"cases": [_run(name, args, info) for name, args, info in CASES]}
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
