#!/usr/bin/env python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live DeckMux demo server — shared Ubuntu bash shell via SSH.

Usage:
    # Start Ubuntu container first:
    #   docker run -d --name uterm-ubuntu -p 2223:22 ubuntu:22.04 bash -c \
    #     "apt-get update -q && apt-get install -y openssh-server -q && \
    #      echo 'root:demo' | chpasswd && \
    #      sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    #      mkdir -p /run/sshd && /usr/sbin/sshd -D"
    uv run python scripts/deckmux_demo_server.py [--port PORT] [--ssh-port PORT]

Opens a server at http://127.0.0.1:PORT with:
  /app/operator/ubuntu  — full xterm.js UI with DeckMux presence bar

Multiple browsers connecting to the same URL share one bash session.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

sys.path.insert(0, "packages/provide-terminal/src")
sys.path.insert(0, "packages/provide-terminal-deckmux/src")

import provide.terminal.server.connectors.ssh  # noqa: F401  — registers "ssh" connector
from provide.terminal.bridge.hub import TermHub
from provide.terminal.deckmux._hub_mixin import DeckMuxMixin
from provide.terminal.server import create_server_app
from provide.terminal.server.models import (
    AuthConfig,
    RecordingConfig,
    ServerBindConfig,
    ServerConfig,
    SessionDefinition,
)

SESSION_ID = "ubuntu"


class DeckMuxTermHub(DeckMuxMixin, TermHub):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._deckmux_init()


def build_app(port: int, ssh_port: int) -> object:
    definition = SessionDefinition(
        session_id=SESSION_ID,
        display_name="Ubuntu bash (Docker)",
        connector_type="ssh",
        input_mode="open",
        presence=True,
        connector_config={
            "host": "127.0.0.1",
            "port": ssh_port,
            "username": "root",
            "password": "demo",
            "insecure_no_host_check": True,
        },
    )
    config = ServerConfig(
        auth=AuthConfig(mode="dev"),
        server=ServerBindConfig(host="127.0.0.1", port=port),
        sessions=[definition],
        recording=RecordingConfig(enabled_by_default=False),
        session_idle_timeout_s=1800,  # auto-sweep idle sessions after 30 min
    )
    return create_server_app(config, hub_class=DeckMuxTermHub)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--ssh-port", type=int, default=2223)
    args = parser.parse_args()

    app = build_app(args.port, args.ssh_port)

    url = f"http://127.0.0.1:{args.port}/app/operator/{SESSION_ID}"
    print(f"DeckMux Ubuntu shell demo: {url}")
    print(f"  SSH backend: root@127.0.0.1:{args.ssh_port} (password: demo)")
    print("  Open the URL in 3 browser tabs to see DeckMux presence.")
    print("Ctrl-C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
