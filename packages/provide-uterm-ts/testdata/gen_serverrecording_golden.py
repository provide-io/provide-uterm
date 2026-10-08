#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record the reference server's recording read routes, probe by probe.

``GET /api/sessions/{id}/recording``, ``.../recording/entries`` and
``.../recording/download``, asked every way their contract distinguishes:
each pagination and filter shape, every bound the query validation enforces,
an unknown session, no credential, a credential whose scopes leave out
``session.recording.read``, a viewer, and a verb the path does not bind.

The reference runs on an ephemeral port in ``dev_token`` mode, configured with
two sessions that never start: ``recorded``, whose recording is a fixture
written into the recording directory before the server comes up, and
``unrecorded``, which has none and records nothing. The routes are what is
under test, not a live session, so the entries they serve are the fixture's:
the same bytes every run and in every port. The fixture is written into the
corpus, malformed line included, so the port serves exactly the same file.

The one value that differs between runs, the temporary path, is masked with
the same marker ``serverhttp_golden`` uses. Everything else is verbatim.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverrecording_golden.py
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

import httpx2

OUT = Path(__file__).resolve().parent / "serverrecording_golden.json"

VOLATILE = "<volatile>"

#: Response headers the client contract rests on. The download's validators
#: are deterministic because the fixture's modification time is pinned.
KEPT_HEADERS = (
    "content-type",
    "allow",
    "content-disposition",
    "content-length",
    "accept-ranges",
    "content-range",
    "etag",
    "last-modified",
)

#: The fixture's modification time, in nanoseconds: exactly representable as
#: a double, so a port setting it through a seconds API lands on the same
#: instant, and the validators derived from it agree.
FIXTURE_MTIME_NS = 1_700_000_000_500_000_000

#: What a multipart boundary is replaced with. It is random per response.
BOUNDARY = "<boundary>"

SESSION = "/api/sessions/recorded"
BARE = "/api/sessions/unrecorded"

#: The recording ``recorded`` serves, one line each. A crash-truncated line is
#: among them: a reader skips it rather than failing.
FIXTURE: tuple[str, ...] = (
    '{"ts": 1700000000.25, "event": "log_start", "data": {"started_at": 1700000000.25}, "session_id": "recorded"}',
    '{"ts": 1700000000.5, "event": "runtime_started", "data": {"session_id": "recorded"}, "session_id": "recorded"}',
    '{"ts": 1700000001.0, "event": "read", "data": {"type": "snapshot", "screen": "$ ", "raw": "$ ", "raw_bytes_b64": "JCA="}, "session_id": "recorded"}',
    '{"ts": 1700000002.0, "event": "send", "data": {"keys": "ls\\r", "bytes_b64": "bHMN"}, "session_id": "recorded"}',
    '{"ts": 1700000002.5, "event": "read", "data": {"type": "snapshot", "screen": "$ ls\\nfile", "raw": "$ ls\\nfile", "raw_bytes_b64": "JCBscwpmaWxl"}, "session_id": "recorded"}',
    '{"ts": 1700000003.0, "event": "annot',
    '{"ts": 1700000003.5, "event": "send", "data": {"keys": "caf\\u00e9", "bytes_b64": "Y2Fmgg=="}, "session_id": "recorded"}',
    '{"ts": 1700000004.0, "event": "log_stop", "data": {}, "session_id": "recorded"}',
)

CONFIG_TOML = """
[recording]
enabled_by_default = false
directory = "{directory}"

[[sessions]]
session_id = "recorded"
connector_type = "shell"
auto_start = false
recording_enabled = true

[[sessions]]
session_id = "unrecorded"
connector_type = "shell"
auto_start = false
recording_enabled = false
"""

#: Every probe. ``auth`` is ``token`` (the stub IdP's administrator), ``none``,
#: ``viewer`` (the viewer role, which holds the recording capability) or
#: ``no_recording`` (an administrator whose scopes leave it out).
PROBES: tuple[dict[str, Any], ...] = (
    {"id": "meta", "method": "GET", "path": f"{SESSION}/recording", "auth": "token"},
    {"id": "meta_viewer", "method": "GET", "path": f"{SESSION}/recording", "auth": "viewer"},
    {"id": "meta_no_recording_scope", "method": "GET", "path": f"{SESSION}/recording", "auth": "no_recording"},
    {"id": "meta_anonymous", "method": "GET", "path": f"{SESSION}/recording", "auth": "none"},
    {"id": "meta_unknown", "method": "GET", "path": "/api/sessions/no-such-session/recording", "auth": "token"},
    {"id": "meta_wrong_method", "method": "POST", "path": f"{SESSION}/recording", "auth": "token", "json": {}},
    {"id": "entries", "method": "GET", "path": f"{SESSION}/recording/entries", "auth": "token"},
    {"id": "entries_limit", "method": "GET", "path": f"{SESSION}/recording/entries?limit=1", "auth": "token"},
    {"id": "entries_offset", "method": "GET", "path": f"{SESSION}/recording/entries?offset=1&limit=1", "auth": "token"},
    {"id": "entries_offset_zero", "method": "GET", "path": f"{SESSION}/recording/entries?offset=0", "auth": "token"},
    {"id": "entries_offset_past", "method": "GET", "path": f"{SESSION}/recording/entries?offset=1000", "auth": "token"},
    {"id": "entries_event", "method": "GET", "path": f"{SESSION}/recording/entries?event=read", "auth": "token"},
    {"id": "entries_event_none", "method": "GET", "path": f"{SESSION}/recording/entries?event=nosuch", "auth": "token"},
    {"id": "entries_event_empty", "method": "GET", "path": f"{SESSION}/recording/entries?event=", "auth": "token"},
    {"id": "entries_limit_max", "method": "GET", "path": f"{SESSION}/recording/entries?limit=500", "auth": "token"},
    {"id": "entries_limit_zero", "method": "GET", "path": f"{SESSION}/recording/entries?limit=0", "auth": "token"},
    {"id": "entries_limit_over", "method": "GET", "path": f"{SESSION}/recording/entries?limit=501", "auth": "token"},
    {"id": "entries_limit_text", "method": "GET", "path": f"{SESSION}/recording/entries?limit=ten", "auth": "token"},
    {"id": "entries_limit_float", "method": "GET", "path": f"{SESSION}/recording/entries?limit=1.5", "auth": "token"},
    {
        "id": "entries_offset_negative",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?offset=-1",
        "auth": "token",
    },
    {
        "id": "entries_event_long",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?event={'e' * 101}",
        "auth": "token",
    },
    {
        "id": "entries_event_max",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?event={'e' * 100}",
        "auth": "token",
    },
    {
        "id": "entries_limit_repeated",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=1&limit=2",
        "auth": "token",
    },
    {
        "id": "entries_limit_padded",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=%205.0%0A",
        "auth": "token",
    },
    {
        "id": "entries_limit_underscore",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=1_0",
        "auth": "token",
    },
    {"id": "entries_limit_signed", "method": "GET", "path": f"{SESSION}/recording/entries?limit=%2B2", "auth": "token"},
    {"id": "entries_limit_empty", "method": "GET", "path": f"{SESSION}/recording/entries?limit=", "auth": "token"},
    {
        "id": "entries_limit_huge",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=99999999999999999999",
        "auth": "token",
    },
    {"id": "entries_limit_fraction", "method": "GET", "path": f"{SESSION}/recording/entries?limit=5.", "auth": "token"},
    {
        "id": "entries_every_error",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?event={'e' * 101}&offset=-1&limit=0",
        "auth": "token",
    },
    {
        "id": "entries_event_astral",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?event={'%F0%9F%98%80' * 100}",
        "auth": "token",
    },
    {
        "id": "entries_event_astral_long",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?event={'%F0%9F%98%80' * 101}",
        "auth": "token",
    },
    {
        "id": "entries_anonymous_bad_query",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=0",
        "auth": "none",
    },
    {
        "id": "entries_no_recording_scope_bad_query",
        "method": "GET",
        "path": f"{SESSION}/recording/entries?limit=0",
        "auth": "no_recording",
    },
    {"id": "entries_viewer", "method": "GET", "path": f"{SESSION}/recording/entries?limit=1", "auth": "viewer"},
    {
        "id": "entries_no_recording_scope",
        "method": "GET",
        "path": f"{SESSION}/recording/entries",
        "auth": "no_recording",
    },
    {"id": "entries_anonymous", "method": "GET", "path": f"{SESSION}/recording/entries", "auth": "none"},
    {
        "id": "entries_unknown",
        "method": "GET",
        "path": "/api/sessions/no-such-session/recording/entries",
        "auth": "token",
    },
    {
        "id": "entries_unknown_bad_query",
        "method": "GET",
        "path": "/api/sessions/no-such-session/recording/entries?limit=0",
        "auth": "token",
    },
    {"id": "download", "method": "GET", "path": f"{SESSION}/recording/download", "auth": "token"},
    {
        "id": "download_range",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=0-9"},
    },
    {
        "id": "download_range_suffix",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=-10"},
    },
    {
        "id": "download_range_open",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=880-"},
    },
    {
        "id": "download_range_past_end",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=880-5000"},
    },
    {
        "id": "download_range_suffix_whole",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=-5000"},
    },
    {
        "id": "download_range_padded",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "BYTES = 1 - 2"},
    },
    {
        "id": "download_ranges",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=20-29,0-4"},
    },
    {
        "id": "download_ranges_overlap",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=0-5,3-9,10-12"},
    },
    {
        "id": "download_ranges_skipped",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=,-,7,a-b,2-3"},
    },
    {
        "id": "download_ranges_too_many",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=" + ",".join(["0-0"] * 101)},
    },
    {
        "id": "download_range_unsatisfiable",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=5000-"},
    },
    {
        "id": "download_range_backwards",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=5-2"},
    },
    {
        "id": "download_range_units",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "items=0-1"},
    },
    {
        "id": "download_range_no_equals",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes"},
    },
    {
        "id": "download_range_empty",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes="},
    },
    {
        "id": "download_if_range_etag",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=0-9", "If-Range": "<etag>"},
    },
    {
        "id": "download_if_range_date",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=0-9", "If-Range": "<last-modified>"},
    },
    {
        "id": "download_if_range_stale",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "token",
        "headers": {"Range": "bytes=0-9", "If-Range": '"stale"'},
    },
    {
        "id": "download_no_recording_scope",
        "method": "GET",
        "path": f"{SESSION}/recording/download",
        "auth": "no_recording",
    },
    {"id": "download_anonymous", "method": "GET", "path": f"{SESSION}/recording/download", "auth": "none"},
    {
        "id": "download_unknown",
        "method": "GET",
        "path": "/api/sessions/no-such-session/recording/download",
        "auth": "token",
    },
    {"id": "meta_malformed_id", "method": "GET", "path": "/api/sessions/bad.id/recording", "auth": "token"},
    {"id": "meta_unicode_id", "method": "GET", "path": "/api/sessions/caf%C3%A9-2/recording", "auth": "token"},
    {
        "id": "entries_malformed_id_bad_query",
        "method": "GET",
        "path": "/api/sessions/a%20b/recording/entries?limit=0",
        "auth": "token",
    },
    {
        "id": "download_malformed_id",
        "method": "GET",
        "path": "/api/sessions/bad.id/recording/download",
        "auth": "token",
    },
    {"id": "meta_malformed_id_anonymous", "method": "GET", "path": "/api/sessions/bad.id/recording", "auth": "none"},
    {"id": "bare_meta", "method": "GET", "path": f"{BARE}/recording", "auth": "token"},
    {"id": "bare_entries", "method": "GET", "path": f"{BARE}/recording/entries", "auth": "token"},
    {"id": "bare_download", "method": "GET", "path": f"{BARE}/recording/download", "auth": "token"},
)

VOLATILE_PATHS: dict[str, tuple[str, ...]] = {
    "meta": ("path",),
    "meta_viewer": ("path",),
}


def _headers(probe: dict[str, Any], tokens: dict[str, str], validators: dict[str, str]) -> dict[str, str]:
    """What a probe's ``auth`` and extra headers mean on the wire."""
    headers = {
        name: validators.get(value.strip("<>"), value) if value.startswith("<") else value
        for name, value in dict(probe.get("headers", {})).items()
    }
    if probe["auth"] != "none":
        headers["Authorization"] = f"Bearer {tokens[str(probe['auth'])]}"
    return headers


def _unbound(response: httpx2.Response, body: Any) -> tuple[dict[str, str], Any]:
    """The kept headers and the body, with a multipart boundary replaced."""
    headers = {name: response.headers[name] for name in KEPT_HEADERS if name in response.headers}
    content_type = headers.get("content-type", "")
    if content_type.startswith("multipart/byteranges; boundary="):
        boundary = content_type.split("=", 1)[1]
        headers["content-type"] = content_type.replace(boundary, BOUNDARY)
        body = body.replace(boundary, BOUNDARY)
    return headers, body


def _mint(config: Any, *, roles: list[str], scope: str | None) -> str:
    """A token signed with the stub IdP's own secret, for a principal it would not mint."""
    import jwt

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "golden-probe",
        "iss": config.auth.jwt_issuer,
        "aud": config.auth.jwt_audience,
        "iat": now,
        "exp": now + 3600,
        config.auth.jwt_roles_claim: roles,
    }
    if scope is not None:
        claims[config.auth.jwt_scopes_claim] = scope
    return jwt.encode(claims, config.auth.jwt_public_key_pem, algorithm="HS256")


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


def _body(response: httpx2.Response) -> Any:
    """The parsed body; a download's file, verbatim; or the one name a body nobody can parse has."""
    if "content-disposition" in response.headers or not response.headers.get("content-type", "").startswith(
        "application/json"
    ):
        return response.text
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
            if httpx2.get(f"http://{host}:{port}/api/health", timeout=1.0).status_code == 200:
                return server
        except httpx2.HTTPError:  # pragma: no cover - the server is still binding
            pass
        time.sleep(0.05)
    raise RuntimeError("the reference server did not become healthy")  # pragma: no cover


def main() -> None:
    logging.disable(logging.WARNING)

    from provide.uterm.server import load_server_config
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.dev_idp import read_dev_token

    scratch = Path(tempfile.mkdtemp(prefix="uterm-golden-"))
    token_path = scratch / "dev_token"
    os.environ["UTERM_DEV_TOKEN_PATH"] = str(token_path)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    host, port = listener.getsockname()

    recordings = scratch / "recordings"
    recordings.mkdir(mode=0o700)
    fixture = recordings / "recorded.jsonl"
    fixture.write_text("".join(line + "\n" for line in FIXTURE), encoding="utf-8")
    os.utime(fixture, ns=(FIXTURE_MTIME_NS, FIXTURE_MTIME_NS))
    config_path = scratch / "server.toml"
    config_path.write_text(CONFIG_TOML.format(directory=recordings), encoding="utf-8")

    config = load_server_config(config_path)
    config.server.host = host
    config.server.port = port
    config.server.public_base_url = f"http://{host}:{port}"
    config.auth.mode = "dev_token"

    app = create_server_app(config)
    tokens = {
        "token": read_dev_token(token_path) or "",
        "viewer": _mint(config, roles=["viewer"], scope=None),
        "no_recording": _mint(config, roles=["admin"], scope="session.read"),
    }
    server = _start(host, port, listener, app)

    records: list[dict[str, Any]] = []
    validators: dict[str, str] = {}
    try:
        with httpx2.Client(base_url=f"http://{host}:{port}", timeout=20.0) as client:
            for probe in PROBES:
                response = client.request(
                    str(probe["method"]),
                    str(probe["path"]),
                    headers=_headers(probe, tokens, validators),
                    json=probe.get("json"),
                )
                if probe["id"] == "download":
                    validators = {name: response.headers[name] for name in ("etag", "last-modified")}
                headers, body = _unbound(response, _body(response))
                records.append(
                    {
                        "id": probe["id"],
                        "method": probe["method"],
                        "path": probe["path"],
                        "auth": probe["auth"],
                        "request_headers": dict(probe.get("headers", {})),
                        "status": response.status_code,
                        "headers": headers,
                        "body": _mask(body, VOLATILE_PATHS.get(str(probe["id"]), ())),
                    }
                )
    finally:
        server.should_exit = True

    payload = {
        "note": (
            "Recorded from the reference FastAPI server on an ephemeral port, in dev_token mode, "
            "serving a fixture recording. Values that differ between runs are masked with "
            f"{VOLATILE!r}."
        ),
        "volatile": VOLATILE,
        "fixture": list(FIXTURE),
        "fixture_mtime_ns": FIXTURE_MTIME_NS,
        "boundary": BOUNDARY,
        "probes": records,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT} ({len(records)} probes)")


if __name__ == "__main__":
    main()
