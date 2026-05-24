#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Server lifecycle and BrowserStep type for demo recording scripts."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from provide.uterm.server import create_server_app, default_server_config

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Per-process token path. Set by start_server() so multiple recorder
# invocations don't fight over ~/.cache/uterm/dev_token (and so the
# recorder doesn't pollute the user's real cache).
_DEV_TOKEN_PATH: Path | None = None


def dev_bearer_headers() -> dict[str, str]:
    """Return ``{"Authorization": "Bearer <token>"}`` for the server start_server() set up.

    Reads the JWT setup_dev_idp() wrote during ``start_server()``. Raises
    if called before ``start_server()`` or if the token file is missing —
    that means the demo's HTTP calls would have hit a 401, so failing
    loudly is better than producing a recording of error responses.
    """
    if _DEV_TOKEN_PATH is None or not _DEV_TOKEN_PATH.is_file():
        raise RuntimeError(
            "dev_bearer_headers() called before start_server() or token file missing — "
            "did the recorder forget to call start_server() first?"
        )
    token = _DEV_TOKEN_PATH.read_text(encoding="utf-8").strip()
    return {"Authorization": f"Bearer {token}"}


# BrowserStep: (url_path | callable(page) | None, wait_seconds, screenshot_name | None)
BrowserStep = tuple[str | Callable[["Page"], None] | None, float, str | None]


def free_port() -> int:
    """Return an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(
    *,
    extra_sessions: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    port: int | None = None,
    hub_class: type | None = None,
) -> tuple[str, Any]:
    """Start a provide-uterm server in a background thread.

    Returns (base_url, uvicorn.Server).
    Pass hub_class to enable optional hub mixins (e.g. DeckMuxTermHub).
    Pass sessions to replace the default session list entirely.
    Pass extra_sessions to append additional sessions to the defaults.
    """
    from provide.uterm.server.models import SessionDefinition

    p = port or free_port()
    base_url = f"http://127.0.0.1:{p}"
    config = default_server_config()
    # Point dev_idp at a per-recorder tmp file so concurrent recordings
    # don't fight over the same token path, and so we don't pollute the
    # user's real ~/.cache/uterm/dev_token. Stash the resolved path in a
    # module-level so dev_bearer_headers() can read it.
    global _DEV_TOKEN_PATH
    _DEV_TOKEN_PATH = Path(tempfile.mkdtemp(prefix="uterm-demo-")) / "dev_token"
    os.environ["UTERM_DEV_TOKEN_PATH"] = str(_DEV_TOKEN_PATH)
    # dev_token: setup_dev_idp() mints an HS256 JWT, writes it to that
    # file, and rewrites config.auth.mode -> "jwt" so the regular JWT
    # validator handles every request. Replaces the unsafe ``dev`` mode
    # (removed in dab4ac2) which used to grant admin without authn.
    config.auth.mode = "dev_token"
    config.server.host = "127.0.0.1"
    config.server.port = p
    config.server.public_base_url = base_url
    config.recording.enabled_by_default = True
    if sessions is not None:
        config.sessions = [SessionDefinition(**s) for s in sessions]
    elif extra_sessions:
        for s in extra_sessions:
            config.sessions.append(SessionDefinition(**s))
    app = create_server_app(config) if hub_class is None else create_server_app(config, hub_class=hub_class)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Server did not start within 15 s")
        time.sleep(0.05)
    return base_url, server


def stop_server(server: Any) -> None:
    """Signal uvicorn to stop."""
    server.should_exit = True
