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
import re
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
#: A step field that is entirely a reference to what an earlier step saw.
REFERENCE = re.compile(r"^\$\{([a-z0-9_]+)\.([A-Za-z0-9_.]+)\}$")


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


def _calls(client: Any, step: dict[str, Any]) -> dict[str, Any]:
    """Every library action, bound to this step's arguments.

    Each one is the method a consumer of the client library would call. The
    point of going through the library rather than building the request here
    is that the library is what is under test.
    """
    session = lambda: str(step.get("session_id"))  # noqa: E731
    worker = lambda: str(step.get("worker_id"))  # noqa: E731
    lease = lambda: str(step.get("hijack_id"))  # noqa: E731
    return {
        "health": lambda: client.health(),
        "list_sessions": lambda: client.list_sessions(),
        "get_session": lambda: client.get_session(session()),
        "session_snapshot": lambda: client.session_snapshot(session()),
        "session_events": lambda: client.session_events(session(), limit=int(step.get("limit", 100))),
        "set_input_mode": lambda: client.set_session_mode(session(), str(step.get("input_mode"))),
        "hijack_acquire": lambda: client.acquire(
            worker(), owner=str(step.get("owner", "operator")), lease_s=int(step.get("lease_s", 90))
        ),
        "hijack_heartbeat": lambda: client.heartbeat(worker(), lease(), lease_s=int(step.get("lease_s", 90))),
        "hijack_send": lambda: client.send(worker(), lease(), keys=str(step.get("keys", ""))),
        "hijack_step": lambda: client.step(worker(), lease()),
        "hijack_snapshot": lambda: client.snapshot(worker(), lease()),
        "hijack_release": lambda: client.release(worker(), lease()),
    }


async def _library_step(step: dict[str, Any], base_url: str, token: str) -> dict[str, Any]:
    """One step performed through the client library a consumer would use."""
    from provide.uterm.client.hijack import HijackClient

    recorder = _Recorder()
    client = HijackClient(base_url, headers=_headers(step.get("auth", "token"), token), transport=recorder)
    async with client:
        ok, _ = await _calls(client, step)[step["action"]]()
    return {"status": recorder.status, "ok": bool(ok), "body": recorder.body, "error": None}


def _resolved(step: dict[str, Any], seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The step with every ``${id.path}`` replaced by what that step saw.

    The harness cannot do this: the driver builds the request, so the driver
    is the only thing holding the value in time to use it. The grammar is one
    step id and one dotted path, and the whole field must be the reference —
    anything else is sent as written.
    """
    resolved = dict(step)
    for key, value in step.items():
        match = REFERENCE.match(value) if isinstance(value, str) else None
        if match is None:
            continue
        fields = seen.get(match.group(1))
        if fields is None:
            raise ValueError(f"step {step['id']!r} refers to {match.group(1)!r}, which has not run")
        found = _dig(fields, match.group(2).split("."))
        if found is _ABSENT:
            raise ValueError(f"step {step['id']!r} refers to {value}, which is not there")
        resolved[key] = found
    return resolved


def _dig(node: Any, segments: list[str]) -> Any:
    """Read a dotted path out of what a step recorded."""
    for segment in segments:
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return _ABSENT
    return node


_ABSENT = object()


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
    seen: dict[str, dict[str, Any]] = {}
    for raw_step in scenario["steps"]:
        # A reference that cannot be resolved is a malformed scenario, not
        # something a server did, so it ends the run rather than becoming an
        # observation the harness would compare.
        step = _resolved(raw_step, seen)
        action = step["action"]
        try:
            if action in {"http_get", "http_post"}:
                fields = await _raw_step(step, base_url, token)
            else:
                fields = await _library_step(step, base_url, token)
        except KeyError as error:
            raise ValueError(f"unknown action {action!r}") from error
        except Exception as error:
            fields = {"status": None, "ok": False, "body": None, "error": f"{type(error).__name__}: {error}"}
        seen[step["id"]] = fields
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
