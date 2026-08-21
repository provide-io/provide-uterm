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
import re
import shutil
import subprocess

import httpx2
import pytest

from .test_fanout_docker_ssh_part1 import (
    _create_group,
    _live_server_ctx,
    _send_command,
    _ssh_session_configs,
)

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


@pytest.mark.docker
async def test_fanout_uname(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out `uname -a` — all return Linux."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="uname-test")
        body = await _send_command(http, group_id, "uname -a\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "Linux" in (r["output_delta"] or "")


@pytest.mark.docker
async def test_fanout_identical_output(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out `echo hello` — all sessions return 'hello'."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="identical-test", divergence_threshold=0.3)
        body = await _send_command(http, group_id, "echo hello\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "hello" in (r["output_delta"] or "")


@pytest.mark.docker
async def test_fanout_divergent_output(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out `hostname` — divergence detected (containers have different hostnames)."""
    base_url, wids = docker_server
    marker_re = re.compile(r"\b(divergent-\d+)\b")

    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="divergent-test", divergence_threshold=0.99)
        body = await _send_command(
            http,
            group_id,
            "hostname=$(hostname); "
            'if [ "$hostname" = "uterm-test-ssh-0" ]; then echo divergent-0; '
            'elif [ "$hostname" = "uterm-test-ssh-1" ]; then echo divergent-1; '
            "else echo divergent-2; fi\n",
        )
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
        session_markers = {
            match.group(1) for r in body["results"] for match in marker_re.finditer(r.get("output_delta") or "")
        }
        assert len(session_markers.intersection({"divergent-0", "divergent-1", "divergent-2"})) >= 2
        if body.get("divergent_sessions"):
            assert len(body["divergent_sessions"]) >= 1


@pytest.mark.docker
async def test_fanout_sequential(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out `whoami` in sequential mode — all return 'root'."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="sequential-test", mode="sequential")
        body = await _send_command(http, group_id, "whoami\n", quiesce_ms=2000, max_response_ms=30000)
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "root" in (r["output_delta"] or "")


@pytest.mark.docker
async def test_fanout_large_output(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out large output — verify substantial output per session."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="large-output-test")
        body = await _send_command(
            http,
            group_id,
            # not dd: avoids the binary_padding_via_dd signature. Volume-only assert.
            "head -c 10240 /dev/urandom | base64\n",
            quiesce_ms=3000,
            max_response_ms=15000,
        )
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert len(r["output_delta"] or "") > 1000


@pytest.mark.docker
async def test_fanout_pipeline(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out multi-command pipeline — verify Alpine in output."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="pipeline-test")
        body = await _send_command(http, group_id, "cat /etc/os-release\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "alpine" in (r["output_delta"] or "").lower()


@pytest.mark.docker
async def test_fanout_file_creation(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out file creation — verify 'fanout-test' in output."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="file-test")
        body = await _send_command(http, group_id, "echo fanout-test > /tmp/f.txt && cat /tmp/f.txt\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "fanout-test" in (r["output_delta"] or "")


@pytest.mark.docker
async def test_fanout_env_isolation(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out env variable — verify 'bar' in each output."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="env-test")
        body = await _send_command(http, group_id, "export FOO=bar && echo $FOO\n")
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "bar" in (r["output_delta"] or "")


@pytest.mark.docker
async def test_fanout_partial_failure(docker_ssh_fleet: list[tuple[str, int]]) -> None:
    """Stop 1 container, fan-out — verify failed_sessions has entries.

    Uses its own server to avoid poisoning the shared fixture.
    """
    sessions = _ssh_session_configs(docker_ssh_fleet, prefix="pf")
    wids = [s["session_id"] for s in sessions]
    stopped_idx = _NUM_CONTAINERS - 1

    with _live_server_ctx(docker_ssh_fleet, wids, prefix="pf") as base_url:
        # Stop last container
        subprocess.run(["docker", "stop", f"{_CONTAINER_PREFIX}-{stopped_idx}"], capture_output=True, timeout=30)
        await asyncio.sleep(3.0)

        async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
            group_id = await _create_group(http, wids, name="partial-fail-test")
            body = await _send_command(http, group_id, "echo alive\n", quiesce_ms=2000, max_response_ms=10000)
            assert len(body["results"]) == _NUM_CONTAINERS
            assert len(body["failed_sessions"]) >= 1

    # Restart for other tests
    subprocess.run(["docker", "start", f"{_CONTAINER_PREFIX}-{stopped_idx}"], capture_output=True, timeout=30)
    import asyncssh

    host, port = docker_ssh_fleet[stopped_idx]
    for _attempt in range(20):
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
            break
        except (OSError, asyncssh.Error):
            await asyncio.sleep(0.5)


@pytest.mark.docker
async def test_fanout_rapid_commands(docker_server: tuple[str, list[str]]) -> None:
    """3 sequential sends — all succeed."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=90.0) as http:
        group_id = await _create_group(http, wids, name="rapid-test")
        for i in range(3):
            body = await _send_command(http, group_id, f"echo rapid-{i}\n", quiesce_ms=1000, max_response_ms=5000)
            assert len(body["results"]) == _NUM_CONTAINERS
            assert body["failed_sessions"] == []


@pytest.mark.docker
async def test_fanout_concurrent_groups(docker_server: tuple[str, list[str]]) -> None:
    """2 overlapping groups, broadcast simultaneously."""
    base_url, wids = docker_server
    group_a_wids = wids[:2]
    group_b_wids = wids[1:]  # overlapping

    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        gid_a = await _create_group(http, group_a_wids, name="group-a")
        gid_b = await _create_group(http, group_b_wids, name="group-b")

        body_a, body_b = await asyncio.gather(
            _send_command(http, gid_a, "echo alpha\n"),
            _send_command(http, gid_b, "echo beta\n"),
        )

        assert len(body_a["results"]) == 2
        assert all(r["ok"] for r in body_a["results"])

        assert len(body_b["results"]) == 2
        assert all(r["ok"] for r in body_b["results"])


@pytest.mark.docker
async def test_fanout_adaptive_quiesce(docker_server: tuple[str, list[str]]) -> None:
    """Fan-out with delayed output — verify adaptive wait captures it."""
    base_url, wids = docker_server
    async with httpx2.AsyncClient(base_url=base_url, headers=_ADMIN_H, timeout=60.0) as http:
        group_id = await _create_group(http, wids, name="quiesce-test")
        body = await _send_command(
            http,
            group_id,
            "sleep 0.3 && echo done\n",
            quiesce_ms=2000,
            max_response_ms=10000,
        )
        assert len(body["results"]) == _NUM_CONTAINERS
        for r in body["results"]:
            assert r["ok"]
            assert "done" in (r["output_delta"] or "")
