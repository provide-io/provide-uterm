#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Server lifecycle and BrowserStep type for demo recording scripts."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import uvicorn
from provide.uterm.server import create_server_app, default_server_config

if TYPE_CHECKING:
    from playwright.sync_api import Page

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
    config.auth.mode = "dev"
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
