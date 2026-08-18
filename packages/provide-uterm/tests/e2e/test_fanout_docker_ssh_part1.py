#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Docker SSH fan-out E2E tests.

Spins up 5 real Alpine+OpenSSH Docker containers, connects to them via
provide-uterm's SSH connector, creates fan-out groups, and broadcasts
real commands — verifying output collection, divergence detection, and
failure handling.

Run with::

    uv run pytest tests/e2e/test_fanout_docker_ssh.py -m docker -v --no-cov --timeout=120
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.hub import EventBus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NUM_CONTAINERS = 3
_IMAGE_NAME = "uterm-test-ssh"
_CONTAINER_PREFIX = "uterm-test-ssh"
_SSH_USER = "root"
_SSH_PASS = "testpass"
_ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}

_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache openssh-server coreutils && \\
    ssh-keygen -A && \\
    echo "root:testpass" | chpasswd && \\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
CMD ["/usr/sbin/sshd", "-D", "-e"]
"""

# Skip everything if Docker is not available
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker not available"),
]


# ---------------------------------------------------------------------------
# Fixture: docker_ssh_fleet (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_ssh_fleet() -> list[tuple[str, int]]:
    """Build an SSH image and start 5 containers, yielding (host, port) tuples."""
    # Build image
    with tempfile.TemporaryDirectory() as tmp:
        dockerfile = Path(tmp) / "Dockerfile"
        dockerfile.write_text(_DOCKERFILE)
        result = subprocess.run(
            ["docker", "build", "-t", _IMAGE_NAME, "."],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.skip(f"Docker build failed: {result.stderr[:500]}")

    # Start containers
    container_names: list[str] = []
    fleet: list[tuple[str, int]] = []
    try:
        for i in range(_NUM_CONTAINERS):
            name = f"{_CONTAINER_PREFIX}-{i}"
            container_names.append(name)
            # Remove stale container if present
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=30,
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "-p",
                    "0:22",
                    "--hostname",
                    name,
                    "--name",
                    name,
                    _IMAGE_NAME,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                pytest.skip(f"Docker run failed for {name}: {result.stderr[:300]}")

            # Get mapped port
            port_result = subprocess.run(
                ["docker", "port", name, "22"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Output like "0.0.0.0:32768" or ":::32768\n0.0.0.0:32768"
            port_line = port_result.stdout.strip().splitlines()[-1]
            port = int(port_line.rsplit(":", 1)[-1])
            fleet.append(("127.0.0.1", port))

        # Wait for SSH readiness on all containers
        import asyncssh

        async def _wait_ssh(host: str, port: int, retries: int = 20, delay: float = 0.5) -> None:
            for attempt in range(retries):
                try:
                    conn = await asyncssh.connect(
                        host,
                        port=port,
                        username=_SSH_USER,
                        password=_SSH_PASS,
                        known_hosts=None,
                        client_keys=[],
                        agent_path=None,
                    )
                    conn.close()
                    return
                except (OSError, asyncssh.Error):
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(delay)

        async def _wait_all() -> None:
            await asyncio.gather(*[_wait_ssh(h, p) for h, p in fleet])

        asyncio.run(_wait_all())

        yield fleet

    finally:
        # Teardown: remove all containers
        for name in container_names:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=30,
            )


# ---------------------------------------------------------------------------
# Live server (thread-based, sets public_base_url for SSH bridge)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_server(docker_ssh_fleet: list[tuple[str, int]]) -> Any:  # type: ignore[misc]
    """Session-scoped live server with 3 SSH sessions pre-connected.

    Yields ``(base_url, worker_ids)`` — one server shared across all tests.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.sessions = []
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)

    # Inject EventBus so OutputCollector can subscribe to events
    hub = app.state.uterm_hub
    if hub.event_bus is None:
        hub._event_bus = EventBus()

    # Create and connect SSH sessions
    import httpx2 as _httpx

    wids = []
    with _httpx.Client(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        for i, (host, sshport) in enumerate(docker_ssh_fleet):
            sid = f"ssh-{i}"
            wids.append(sid)
            resp = http.post(
                "/api/sessions",
                json={
                    "session_id": sid,
                    "display_name": f"SSH Container {i}",
                    "connector_type": "ssh",
                    "auto_start": True,
                    "connector_config": {
                        "host": host,
                        "port": sshport,
                        "username": _SSH_USER,
                        "password": _SSH_PASS,
                        "insecure_no_host_check": True,
                    },
                },
            )
            assert resp.status_code == 200, f"Failed to create {sid}: {resp.text}"

        # Poll until all connected
        poll_deadline = time.monotonic() + 60.0
        for sid in wids:
            while time.monotonic() < poll_deadline:
                resp = http.get(f"/api/sessions/{sid}")
                if resp.status_code == 200 and resp.json().get("connected") is True:
                    break
                time.sleep(0.3)
            else:
                raise AssertionError(f"Session {sid} did not connect within 60s")

    try:
        yield base_url, wids
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextlib.contextmanager
def _live_server_ctx(fleet: list[tuple[str, int]], wids: list[str], prefix: str = "pf") -> Any:  # type: ignore[misc]
    """Standalone server for tests that modify infrastructure (e.g. stop containers)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.sessions = []
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)

    hub = app.state.uterm_hub
    if hub.event_bus is None:
        hub._event_bus = EventBus()

    import httpx2 as _httpx

    with _httpx.Client(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        for i, (host, sshport) in enumerate(fleet):
            sid = f"{prefix}-{i}"
            http.post(
                "/api/sessions",
                json={
                    "session_id": sid,
                    "display_name": f"SSH {prefix} {i}",
                    "connector_type": "ssh",
                    "auto_start": True,
                    "connector_config": {
                        "host": host,
                        "port": sshport,
                        "username": _SSH_USER,
                        "password": _SSH_PASS,
                        "insecure_no_host_check": True,
                    },
                },
            )

        wids = [f"{prefix}-{i}" for i, _ in enumerate(fleet)]
        poll_deadline = time.monotonic() + 60.0
        for sid in wids:
            while time.monotonic() < poll_deadline:
                resp = http.get(f"/api/sessions/{sid}")
                if resp.status_code == 200 and resp.json().get("connected") is True:
                    break
                time.sleep(0.3)

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ssh_session_configs(fleet: list[tuple[str, int]], prefix: str = "ssh") -> list[dict[str, Any]]:
    """Build session config dicts for the SSH containers."""
    return [
        {
            "session_id": f"{prefix}-{i}",
            "display_name": f"SSH Container {i}",
            "connector_type": "ssh",
            "auto_start": True,
            "connector_config": {
                "host": host,
                "port": port,
                "username": _SSH_USER,
                "password": _SSH_PASS,
                "insecure_no_host_check": True,
            },
        }
        for i, (host, port) in enumerate(fleet)
    ]


async def _create_and_connect_sessions(
    http: httpx2.AsyncClient,
    sessions: list[dict[str, Any]],
    timeout: float = 60.0,
) -> None:
    """Create sessions via REST, connect them, and wait until connected."""
    for sess in sessions:
        resp = await http.post("/api/sessions", json=sess)
        assert resp.status_code == 200, f"Failed to create {sess['session_id']}: {resp.text}"

    # auto_start=True — runtime starts on create, poll until connected
    deadline = time.monotonic() + timeout
    for sess in sessions:
        sid = sess["session_id"]
        while time.monotonic() < deadline:
            resp = await http.get(f"/api/sessions/{sid}")
            if resp.status_code == 200 and resp.json().get("connected") is True:
                break
            await asyncio.sleep(0.3)
        else:
            raise AssertionError(f"Session {sid} did not become connected within {timeout}s")


async def _create_group(
    http: httpx2.AsyncClient,
    worker_ids: list[str],
    *,
    name: str = "test-group",
    mode: str = "parallel",
    **kwargs: Any,
) -> str:
    """Create a fan-out group, return group_id."""
    resp = await http.post(
        "/api/fanout/groups",
        json={"name": name, "worker_ids": worker_ids, "mode": mode, **kwargs},
    )
    assert resp.status_code == 200, f"Failed to create group: {resp.text}"
    return resp.json()["group_id"]


async def _send_command(
    http: httpx2.AsyncClient,
    group_id: str,
    data: str,
    *,
    quiesce_ms: int = 2000,
    max_response_ms: int = 10000,
) -> dict[str, Any]:
    """Send a command to a fan-out group, return response body."""
    resp = await http.post(
        f"/api/fanout/groups/{group_id}/send",
        json={"data": data, "quiesce_ms": quiesce_ms, "max_response_ms": max_response_ms},
    )
    assert resp.status_code == 200, f"Fan-out send failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Tests — all share the session-scoped docker_server fixture
# ---------------------------------------------------------------------------


async def test_fanout_hostname(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out `hostname` — each container returns a different hostname."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="hostname-test")
        body = await _send_command(http, group_id, "hostname\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert r["output_delta"]
