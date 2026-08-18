#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the lease and snapshot routes.

The sibling ``gen_serverhttp_golden.py`` records the *read* half of the session
API as a set of independent probes. This one records the half that has state:
the session snapshot, the input mode, and the whole hijack lease lifecycle —
take it, keep it alive, read through it, give it back, and every way of being
refused one.

It has to be a *sequence* rather than a set, because each answer depends on
what the ones before it did: a lease cannot be taken until the mode has moved,
a second acquire is only a conflict while the first is held, and a release only
404s the second time. So the probes run in order and a later one may quote an
earlier one's answer with ``${id.dotted.path}`` — the same grammar the live
scenarios use, for the same reason.

Recorded against a real uvicorn on an ephemeral port, in ``dev_token`` mode,
with the default configuration — the same way the live driver runs the
reference. That matters more here than anywhere else: a lease is granted
against a *worker*, and the worker only exists because the lifespan started the
configured session and connected it back over its own WebSocket. An in-process
ASGI transport would leave that half-done and every acquire would be refused
with "No worker connected", which is exactly the answer the corpus must not
record.

Two envelopes appear below and both are the reference's. The lease routes
answer with ``error``; the session routes answer with ``detail``. A port that
picked one for everything would be wrong half the time, which is why every
refusal here is recorded whole rather than by its status alone.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverhijack_golden.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx2

OUT = Path(__file__).resolve().parent / "serverhijack_golden.json"

#: What a value that differs between runs is replaced with. The harness's own
#: marker, so a reader comparing the two sees the same word.
VOLATILE = "<volatile>"

#: One step id and one dotted path, and the whole field has to be the
#: reference — anything else is sent as written. The live driver's grammar.
REFERENCE = re.compile(r"^\$\{([A-Za-z0-9_]+)\.([A-Za-z0-9_.]+)\}$")

#: Response headers worth recording. The rest are a clock, a request id or a
#: security header the configuration decides.
KEPT_HEADERS = ("content-type",)

#: The session the default configuration defines, and the worker it becomes.
SESSION = "provide-shell"

#: Every probe, in order. ``volatile`` names the paths of *this* probe's body
#: that differ between runs; ``keys`` asks for the body's key set to be
#: recorded as well, for a body masked whole.
PROBES: tuple[dict[str, Any], ...] = (
    {
        "id": "snapshot_before_hijack",
        "method": "GET",
        "path": f"/api/sessions/{SESSION}/snapshot",
        "auth": "token",
        "volatile": ("*",),
        "keys": True,
    },
    {
        "id": "snapshot_anonymous",
        "method": "GET",
        "path": f"/api/sessions/{SESSION}/snapshot",
        "auth": "none",
    },
    {
        "id": "snapshot_unknown_session",
        "method": "GET",
        "path": "/api/sessions/no-such-session/snapshot",
        "auth": "token",
    },
    {
        "id": "acquire_while_open",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/acquire",
        "auth": "token",
        "json": {"owner": "conformance", "lease_s": 60},
    },
    {
        "id": "acquire_unknown_worker",
        "method": "POST",
        "path": "/worker/no-such-worker/hijack/acquire",
        "auth": "token",
        "json": {"owner": "conformance", "lease_s": 60},
    },
    {
        "id": "acquire_anonymous",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/acquire",
        "auth": "none",
        "json": {"owner": "nobody", "lease_s": 60},
    },
    {
        "id": "mode_undefined",
        "method": "POST",
        "path": f"/api/sessions/{SESSION}/mode",
        "auth": "token",
        "json": {"input_mode": "sideways"},
    },
    {
        "id": "mode_missing",
        "method": "POST",
        "path": f"/api/sessions/{SESSION}/mode",
        "auth": "token",
        "json": {},
    },
    {
        "id": "mode_anonymous",
        "method": "POST",
        "path": f"/api/sessions/{SESSION}/mode",
        "auth": "none",
        "json": {"input_mode": "hijack"},
    },
    {
        "id": "mode_unknown_session",
        "method": "POST",
        "path": "/api/sessions/no-such-session/mode",
        "auth": "token",
        "json": {"input_mode": "hijack"},
    },
    {
        "id": "to_hijack",
        "method": "POST",
        "path": f"/api/sessions/{SESSION}/mode",
        "auth": "token",
        "json": {"input_mode": "hijack"},
        "volatile": ("created_at", "connected", "lifecycle_state"),
    },
    # The worker's own route onto the same field. It sits behind the same gate
    # as the lease routes rather than the session ones, and it asks for
    # ``session.control.mode`` — so its refusals are the gate's ``detail``
    # while the hub's own two are the lease routes' ``error``. Both envelopes
    # appear below and both are the reference's.
    #
    # This one re-asserts the mode the session is already in, which is what a
    # worker does on reconnect and must not be a refusal. It also leaves the
    # session in ``hijack`` for the acquire that follows.
    {
        "id": "worker_mode_noop_hijack",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
        "json": {"input_mode": "hijack"},
    },
    {
        "id": "worker_mode_anonymous",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "none",
        "json": {"input_mode": "open"},
    },
    {
        "id": "worker_mode_unknown_worker",
        "method": "POST",
        "path": "/worker/no-such-worker/input_mode",
        "auth": "token",
        "json": {"input_mode": "open"},
    },
    {
        "id": "worker_mode_undefined",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
        "json": {"input_mode": "sideways"},
    },
    {
        "id": "worker_mode_malformed_worker_id",
        "method": "POST",
        "path": "/worker/not%20a%20worker/input_mode",
        "auth": "token",
        "json": {"input_mode": "open"},
    },
    {
        "id": "worker_mode_wrong_method",
        "method": "GET",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
    },
    {
        "id": "acquire",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/acquire",
        "auth": "token",
        "json": {"owner": "conformance", "lease_s": 60},
        "volatile": ("hijack_id", "lease_expires_at"),
    },
    # The refusal this route exists for, and the transition it still allows
    # while the lease is held. ``second_acquire`` below is what proves the
    # refusal left the lease alone rather than answering 409 and writing the
    # field anyway — the answer alone shows what was said, not what was stored.
    {
        "id": "worker_mode_open_while_held",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
        "json": {"input_mode": "open"},
    },
    {
        "id": "worker_mode_hijack_while_held",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
        "json": {"input_mode": "hijack"},
    },
    {
        "id": "second_acquire",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/acquire",
        "auth": "token",
        "json": {"owner": "second", "lease_s": 60},
    },
    {
        "id": "heartbeat",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/heartbeat",
        "auth": "token",
        "json": {"lease_s": 60},
        "volatile": ("hijack_id", "lease_expires_at"),
    },
    {
        "id": "heartbeat_unknown_lease",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/00000000-0000-0000-0000-000000000000/heartbeat",
        "auth": "token",
        "json": {"lease_s": 60},
    },
    {
        "id": "hijack_snapshot",
        "method": "GET",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/snapshot",
        "auth": "token",
        "volatile": ("*",),
        "keys": True,
    },
    {
        "id": "hijack_snapshot_unknown_lease",
        "method": "GET",
        "path": f"/worker/{SESSION}/hijack/00000000-0000-0000-0000-000000000000/snapshot",
        "auth": "token",
    },
    {
        "id": "hijack_send",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/send",
        "auth": "token",
        "json": {"keys": "/say hello\r"},
        "volatile": ("hijack_id", "lease_expires_at"),
    },
    {
        "id": "hijack_step",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/step",
        "auth": "token",
        "volatile": ("hijack_id", "lease_expires_at"),
    },
    {
        "id": "hijack_send_unknown_lease",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/00000000-0000-0000-0000-000000000000/send",
        "auth": "token",
        "json": {"keys": "x"},
    },
    {
        "id": "malformed_worker_id",
        "method": "POST",
        "path": "/worker/not%20a%20worker/hijack/acquire",
        "auth": "token",
        "json": {"owner": "conformance", "lease_s": 60},
    },
    {
        "id": "malformed_hijack_id",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/NOT-A-UUID/heartbeat",
        "auth": "token",
        "json": {"lease_s": 60},
    },
    {
        "id": "hijack_unknown_path",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/not-a-verb",
        "auth": "token",
        "json": {},
    },
    {
        "id": "session_snapshot_while_held",
        "method": "GET",
        "path": f"/api/sessions/{SESSION}/snapshot",
        "auth": "token",
        "volatile": ("*",),
        "keys": True,
    },
    {
        "id": "release",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/release",
        "auth": "token",
        "volatile": ("hijack_id",),
    },
    {
        "id": "release_again",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire.body.hijack_id}}/release",
        "auth": "token",
    },
    {
        "id": "acquire_after_release",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/acquire",
        "auth": "token",
        "json": {"owner": "third", "lease_s": 60},
        "volatile": ("hijack_id", "lease_expires_at"),
    },
    {
        "id": "release_third",
        "method": "POST",
        "path": f"/worker/{SESSION}/hijack/${{acquire_after_release.body.hijack_id}}/release",
        "auth": "token",
        "volatile": ("hijack_id",),
    },
    {
        "id": "to_open",
        "method": "POST",
        "path": f"/api/sessions/{SESSION}/mode",
        "auth": "token",
        "json": {"input_mode": "open"},
        "volatile": ("created_at", "connected", "lifecycle_state"),
    },
    # The other no-op, from the other side: nothing is held and the session is
    # already open, so there is no guard to trip and no field to move.
    {
        "id": "worker_mode_noop_open",
        "method": "POST",
        "path": f"/worker/{SESSION}/input_mode",
        "auth": "token",
        "json": {"input_mode": "open"},
    },
)

#: A bearer token no server issued, for the probes that ask what an impostor
#: gets. The same shape the live driver's ``auth: "bad"`` sends.
FORGED_TOKEN = "uterm-live-conformance-token-no-server-issued"  # noqa: S105  # pragma: allowlist secret


def _headers(auth: str, token: str) -> dict[str, str]:
    """What a probe's ``auth`` means on the wire."""
    if auth == "none":
        return {}
    if auth == "bad":
        return {"Authorization": f"Bearer {FORGED_TOKEN}"}
    return {"Authorization": f"Bearer {token}"}


def _mask(value: Any, paths: tuple[str, ...]) -> Any:
    """A copy of *value* with every declared path replaced."""
    masked = copy.deepcopy(value)
    for path in paths:
        if path == "*":
            return VOLATILE
        _mask_one(masked, path.split("."))
    return masked


def _mask_one(node: Any, segments: list[str]) -> None:
    head, rest = segments[0], segments[1:]
    if not isinstance(node, dict) or head not in node:
        return
    if rest:
        _mask_one(node[head], rest)
    else:
        node[head] = VOLATILE


def _body(response: httpx2.Response) -> Any:
    """The parsed body, or the one name a body nobody can parse has."""
    try:
        return response.json()
    except ValueError:
        return "<non-json>"


def _dig(node: Any, segments: list[str]) -> Any:
    """Read a dotted path out of what an earlier probe saw."""
    for segment in segments:
        if not isinstance(node, dict) or segment not in node:
            raise KeyError(".".join(segments))
        node = node[segment]
    return node


def _resolve(path: str, seen: dict[str, dict[str, Any]]) -> str:
    """A path with one ``${id.dotted.path}`` reference substituted.

    The reference is resolved against what the probe *actually observed*, not
    against the masked copy that is written out — a hijack id is masked in the
    corpus precisely because it is generated, and the next request still has
    to quote the real one.
    """
    parts = path.split("/")
    resolved = []
    for part in parts:
        match = REFERENCE.match(part)
        resolved.append(str(_dig(seen[match.group(1)], match.group(2).split("."))) if match else part)
    return "/".join(resolved)


def _start(host: str, port: int, listener: socket.socket, app: Any) -> Any:
    """Run uvicorn on an already-bound socket, on its own thread."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"http://{host}:{port}/api/health", timeout=1.0).status_code == 200:
                return server
        except httpx2.HTTPError:  # pragma: no cover - the server is still binding
            pass
        time.sleep(0.05)
    raise RuntimeError("the reference server did not become healthy")  # pragma: no cover


def _await_worker(client: httpx2.Client, token: str) -> None:
    """Wait until the configured session's worker has attached to the hub.

    Without this the first acquire races the lifespan's own connect and the
    corpus records "No worker connected for this session." — a true answer to
    a question nobody asked.
    """
    deadline = time.monotonic() + 30.0
    headers = _headers("token", token)
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{SESSION}", headers=headers)
        if response.status_code == 200 and response.json().get("connected") is True:
            return
        time.sleep(0.1)
    raise RuntimeError("the reference session's worker never connected")  # pragma: no cover


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
    seen: dict[str, dict[str, Any]] = {}
    try:
        with httpx2.Client(base_url=f"http://{host}:{port}", timeout=20.0) as client:
            _await_worker(client, token)
            for probe in PROBES:
                path = _resolve(str(probe["path"]), seen)
                response = client.request(
                    str(probe["method"]),
                    path,
                    headers=_headers(str(probe["auth"]), token),
                    json=probe.get("json"),
                )
                body = _body(response)
                seen[str(probe["id"])] = {"status": response.status_code, "body": body}
                record: dict[str, Any] = {
                    "id": probe["id"],
                    "method": probe["method"],
                    "path": probe["path"],
                    "auth": probe["auth"],
                    "status": response.status_code,
                    "headers": {name: response.headers[name] for name in KEPT_HEADERS if name in response.headers},
                    "body": _mask(body, tuple(probe.get("volatile", ()))),
                }
                if probe.get("json") is not None:
                    record["json"] = probe["json"]
                if probe.get("keys") and isinstance(body, dict):
                    record["body_keys"] = sorted(body)
                records.append(record)
    finally:
        server.should_exit = True

    payload = {
        "note": (
            "Recorded in order from the reference FastAPI server on an ephemeral port, in dev_token "
            "mode, with the default configuration and its session's worker actually attached. A probe "
            f"may quote an earlier one with ${{id.path}}. Values that differ between runs are {VOLATILE!r}; "
            "'body_keys' is the key set of a body that was masked whole."
        ),
        "volatile": VOLATILE,
        "session_id": SESSION,
        "probes": records,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT} ({len(records)} probes)")


if __name__ == "__main__":
    main()
