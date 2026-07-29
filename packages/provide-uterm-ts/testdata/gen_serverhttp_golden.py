#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the server's HTTP surface.

Every other corpus in this directory records what a *function* decides. This
one records what the reference *server* answers, because that is the thing the
TypeScript server has to be indistinguishable from: the live conformance
matrix (``conformance/live/``) compares a cell field-for-field against the
Python cell, so a field nobody wrote an expectation for still has to agree.

It is generated the way the live driver runs the reference — a real uvicorn on
an ephemeral port, the real ``create_server_app``, the real ``dev_token`` stub
IdP — rather than through an in-process ASGI transport. The lifespan seeds the
configured session and connects it back to the server over its own WebSocket,
and an in-process transport would leave that half-done, which is exactly the
state the corpus must not record.

What is deliberately *not* recorded is anything that legitimately differs
between two runs: the package version, the uptime, the session count and a
session's ``created_at``/``lifecycle_state``/``connected``. These are the
paths the scenarios themselves declare volatile, masked here with the same
marker the harness uses. Everything else — every status code, every field
name, every value — is recorded verbatim, because that is what has to match.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverhttp_golden.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

OUT = Path(__file__).resolve().parent / "serverhttp_golden.json"

#: What a value that differs between runs is replaced with. The harness's own
#: marker, so a reader comparing the two sees the same word.
VOLATILE = "<volatile>"

#: A bearer token no server issued, for the probes that ask what an impostor
#: gets. The same shape the live driver's ``auth: "bad"`` sends.
FORGED_TOKEN = "uterm-live-conformance-token-no-server-issued"  # noqa: S105  # pragma: allowlist secret

#: Response headers worth recording. The rest are either a clock, a request id
#: or a security header the config decides, none of which the client contract
#: rests on.
KEPT_HEADERS = ("content-type", "allow")

#: Every probe, and which paths of its body are a clock or a counter.
PROBES: tuple[dict[str, Any], ...] = (
    {"id": "health_anonymous", "method": "GET", "path": "/api/health", "auth": "none"},
    {"id": "health_token", "method": "GET", "path": "/api/health", "auth": "token"},
    {"id": "health_forged", "method": "GET", "path": "/api/health", "auth": "bad"},
    {"id": "sessions_anonymous", "method": "GET", "path": "/api/sessions", "auth": "none"},
    {"id": "sessions_forged", "method": "GET", "path": "/api/sessions", "auth": "bad"},
    {"id": "sessions_bare_scheme", "method": "GET", "path": "/api/sessions", "auth": "bare"},
    {"id": "sessions_not_bearer", "method": "GET", "path": "/api/sessions", "auth": "basic"},
    {"id": "sessions_token", "method": "GET", "path": "/api/sessions", "auth": "token"},
    {"id": "session_token", "method": "GET", "path": "/api/sessions/provide-shell", "auth": "token"},
    {"id": "session_unknown", "method": "GET", "path": "/api/sessions/no-such-session", "auth": "token"},
    {"id": "session_unknown_anonymous", "method": "GET", "path": "/api/sessions/no-such-session", "auth": "none"},
    {"id": "unknown_path", "method": "GET", "path": "/api/not-a-thing", "auth": "token"},
    {"id": "unknown_path_anonymous", "method": "GET", "path": "/api/not-a-thing", "auth": "none"},
    {"id": "wrong_method_health", "method": "POST", "path": "/api/health", "auth": "token", "json": {}},
    {"id": "wrong_method_session", "method": "PUT", "path": "/api/sessions/provide-shell", "auth": "token", "json": {}},
    {"id": "healthz", "method": "GET", "path": "/healthz", "auth": "none"},
    {"id": "readyz", "method": "GET", "path": "/readyz", "auth": "none"},
)

#: Dotted paths, one per probe id, whose value is a clock, a counter or a
#: state that depends on how far startup got. ``*`` stands for every element
#: of a list, as it does in a scenario's ``volatile``.
VOLATILE_PATHS: dict[str, tuple[str, ...]] = {
    "health_anonymous": ("version", "uptime_s", "active_sessions"),
    "health_token": ("version", "uptime_s", "active_sessions"),
    "health_forged": ("version", "uptime_s", "active_sessions"),
    "sessions_token": ("*.created_at", "*.lifecycle_state", "*.connected"),
    "session_token": ("created_at", "lifecycle_state", "connected"),
}


def _headers(auth: str, token: str) -> dict[str, str]:
    """What a probe's ``auth`` means on the wire."""
    if auth == "none":
        return {}
    if auth == "bad":
        return {"Authorization": f"Bearer {FORGED_TOKEN}"}
    if auth == "bare":
        # A scheme with nothing after it: the reference splits on the first
        # space and refuses anything that is not two parts.
        return {"Authorization": "Bearer"}
    if auth == "basic":
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {token}"}


def _mask(value: Any, paths: tuple[str, ...]) -> Any:
    """A copy of *value* with every declared path replaced."""
    masked = copy.deepcopy(value)
    for path in paths:
        _mask_one(masked, path.split("."))
    return masked


def _mask_one(node: Any, segments: list[str]) -> None:
    head, rest = segments[0], segments[1:]
    keys: list[Any]
    if head == "*":
        keys = list(range(len(node))) if isinstance(node, list) else list(node)
    elif isinstance(node, dict) and head in node:
        keys = [head]
    else:
        keys = []
    for key in keys:
        if rest:
            _mask_one(node[key], rest)
        else:
            node[key] = VOLATILE


def _body(response: httpx.Response) -> Any:
    """The parsed body, or the one name a body nobody can parse has."""
    try:
        return response.json()
    except ValueError:
        return "<non-json>"


def _start(host: str, port: int, listener: socket.socket, app: Any) -> Any:
    """Run uvicorn on an already-bound socket, on its own thread."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://{host}:{port}/api/health", timeout=1.0).status_code == 200:
                return server
        except httpx.HTTPError:  # pragma: no cover - the server is still binding
            pass
        time.sleep(0.05)
    raise RuntimeError("the reference server did not become healthy")  # pragma: no cover


def main() -> None:
    # The reference logs a security-posture report and one line per request at
    # info; neither belongs in a generator's output.
    logging.disable(logging.WARNING)

    from provide.uterm.server import load_server_config
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.dev_idp import read_dev_token

    token_path = Path(tempfile.mkdtemp(prefix="uterm-golden-")) / "dev_token"
    os.environ["UTERM_DEV_TOKEN_PATH"] = str(token_path)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    host, port = listener.getsockname()

    config = load_server_config(None)
    config.server.host = host
    config.server.port = port
    config.server.public_base_url = f"http://{host}:{port}"
    config.auth.mode = "dev_token"

    app = create_server_app(config)
    token = read_dev_token(token_path) or ""
    server = _start(host, port, listener, app)

    records: list[dict[str, Any]] = []
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=20.0) as client:
            for probe in PROBES:
                response = client.request(
                    str(probe["method"]),
                    str(probe["path"]),
                    headers=_headers(str(probe["auth"]), token),
                    json=probe.get("json"),
                )
                records.append(
                    {
                        "id": probe["id"],
                        "method": probe["method"],
                        "path": probe["path"],
                        "auth": probe["auth"],
                        "status": response.status_code,
                        "headers": {name: response.headers[name] for name in KEPT_HEADERS if name in response.headers},
                        "body": _mask(_body(response), VOLATILE_PATHS.get(str(probe["id"]), ())),
                    }
                )
    finally:
        server.should_exit = True

    payload = {
        "note": (
            "Recorded from the reference FastAPI server on an ephemeral port, in dev_token mode, "
            "with the default configuration. Values that differ between runs are masked with "
            f"{VOLATILE!r} — the same paths the live scenarios declare volatile."
        ),
        "volatile": VOLATILE,
        "probes": records,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT} ({len(records)} probes)")


if __name__ == "__main__":
    main()
