#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The Python driver for the live conformance harness.

Speaks the protocol in ``conformance/live/PROTOCOL.md``: ``serve`` stands the
reference FastAPI server up on an ephemeral port and announces it; ``client``
runs a scenario's steps against a base URL and writes down what it saw.

It observes, it does not judge. Every expectation is evaluated by the harness,
in one implementation, so that four languages cannot disagree about what an
expectation *means* — only about what their server did.

Usage::

    driver.py serve [--auth dev_token]
    driver.py client --base-url URL --token TOKEN --scenario FILE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

LANGUAGE = "python"
#: What this driver can do, in the vocabulary scenarios use to require things.
CAPABILITIES = ("status.observed", "hijack.ws", "hijack.rest")
#: A token no server issued, for steps that ask what an impostor gets.
FORGED_TOKEN = "not.a.real.token"  # noqa: S105  # pragma: allowlist secret
#: What a body that is not JSON is recorded as, in every language.
NON_JSON = "<non-json>"


# --------------------------------------------------------------------------
# server role
# --------------------------------------------------------------------------


def serve(auth: str) -> int:
    """Stand the reference server up on an ephemeral port and announce it."""
    import uvicorn

    from provide.uterm.server import load_server_config
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.dev_idp import read_dev_token

    token_dir = Path(tempfile.mkdtemp(prefix="uterm-live-"))
    token_path = token_dir / "dev_token"
    os.environ["UTERM_DEV_TOKEN_PATH"] = str(token_path)

    # Bind first, announce the port the operating system chose. Nothing in
    # this harness may name a port.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    host, port = listener.getsockname()

    config = load_server_config(None)
    config.server.host = host
    config.server.port = port
    config.server.public_base_url = f"http://{host}:{port}"
    config.auth.mode = auth

    app = create_server_app(config)
    token = read_dev_token(token_path) or ""

    _announce(
        {
            "role": "server",
            "language": LANGUAGE,
            "base_url": f"http://{host}:{port}",
            "token": token,
            "capabilities": list(CAPABILITIES),
        }
    )

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    stopper = _stop_on_stdin_close(server)
    server.run(sockets=[listener])
    stopper.join(timeout=1.0)
    return 0


def _stop_on_stdin_close(server: Any) -> Any:
    """Watch stdin; closing it is how the harness asks for a clean stop."""
    import threading

    def wait() -> None:
        sys.stdin.read()
        server.should_exit = True

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------
# client role
# --------------------------------------------------------------------------


class _Recorder(httpx.AsyncBaseTransport):
    """The status a client library answers ``(ok, body)`` over.

    Every port's client drops the status code, so a 401, a 403 and a 404 all
    arrive as the same refusal. The library still makes the call; this only
    writes down what came back.
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.status: int | None = None
        self.body: Any = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        await response.aread()
        self.status = response.status_code
        self.body = _body_of(response)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _body_of(response: httpx.Response) -> Any:
    """The response body as JSON, or the one name a non-JSON body has."""
    try:
        return response.json()
    except ValueError:
        return NON_JSON


def _headers(auth: str, token: str) -> dict[str, str]:
    """What a step's ``auth`` means on the wire."""
    if auth == "none":
        return {}
    if auth == "bad":
        return {"Authorization": f"Bearer {FORGED_TOKEN}"}
    return {"Authorization": f"Bearer {token}"}


async def _library_step(step: dict[str, Any], base_url: str, token: str) -> dict[str, Any]:
    """One step performed through the client library a consumer would use."""
    from provide.uterm.client.hijack import HijackClient

    recorder = _Recorder()
    client = HijackClient(base_url, headers=_headers(step.get("auth", "token"), token), transport=recorder)
    calls = {
        "health": lambda: client.health(),
        "list_sessions": lambda: client.list_sessions(),
        "get_session": lambda: client.get_session(str(step.get("session_id"))),
        "session_snapshot": lambda: client.session_snapshot(str(step.get("session_id"))),
    }
    async with client:
        ok, _ = await calls[step["action"]]()
    return {"status": recorder.status, "ok": bool(ok), "body": recorder.body, "error": None}


async def _raw_step(step: dict[str, Any], base_url: str, token: str) -> dict[str, Any]:
    """One step performed straight over HTTP, for surfaces no method reaches."""
    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
        headers = _headers(step.get("auth", "token"), token)
        if step["action"] == "http_get":
            response = await client.get(str(step["path"]), headers=headers)
        else:
            response = await client.post(str(step["path"]), headers=headers, json=step.get("body"))
    return {
        "status": response.status_code,
        "ok": response.is_success,
        "body": _body_of(response),
        "error": None,
    }


async def _run_steps(scenario: dict[str, Any], base_url: str, token: str) -> list[dict[str, Any]]:
    """Every step in order, each recorded whatever it did."""
    steps: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        action = step["action"]
        try:
            if action in {"http_get", "http_post"}:
                fields = await _raw_step(step, base_url, token)
            elif action in {"health", "list_sessions", "get_session", "session_snapshot"}:
                fields = await _library_step(step, base_url, token)
            else:
                raise ValueError(f"unknown action {action!r}")
        except Exception as error:
            fields = {"status": None, "ok": False, "body": None, "error": f"{type(error).__name__}: {error}"}
        steps.append({"id": step["id"], "fields": fields})
    return steps


def run_client(base_url: str, token: str, scenario_path: Path) -> int:
    """Run a scenario and write down what happened."""
    scenario = json.loads(scenario_path.read_text())
    missing = [needed for needed in scenario.get("requires", ()) if needed not in CAPABILITIES]
    if missing:
        _announce(_result(scenario, "unsupported", [], f"missing capabilities: {', '.join(missing)}"))
        return 0
    try:
        steps = asyncio.run(_run_steps(scenario, base_url, token))
    except Exception as error:
        _announce(_result(scenario, "error", [], f"{type(error).__name__}: {error}"))
        return 1
    _announce(_result(scenario, "completed", steps, None))
    return 0


def _result(scenario: dict[str, Any], status: str, steps: list[dict[str, Any]], error: str | None) -> dict[str, Any]:
    return {
        "scenario_id": scenario.get("id", ""),
        "language": LANGUAGE,
        "role": "client",
        "status": status,
        "capabilities": list(CAPABILITIES),
        "steps": steps,
        "error": error,
    }


def _announce(payload: dict[str, Any]) -> None:
    """One line of JSON on stdout, which is the whole of the protocol."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="provide-uterm live conformance driver (python)")
    sub = parser.add_subparsers(dest="role", required=True)

    serve_p = sub.add_parser("serve", help="run the reference server on an ephemeral port")
    serve_p.add_argument("--auth", default="dev_token")

    client_p = sub.add_parser("client", help="run a scenario against a running server")
    client_p.add_argument("--base-url", required=True)
    client_p.add_argument("--token", default="")
    client_p.add_argument("--scenario", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.role == "serve":
        return serve(args.auth)
    return run_client(args.base_url, args.token, args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
