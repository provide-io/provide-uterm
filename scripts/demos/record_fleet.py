#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Spawn 9 fleet shell workers, register with the External Management Tier, broadcast a deploy command."""

from __future__ import annotations

import asyncio
import contextlib
import random as _random
import sys
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    asciinema_record,
    banner,
    dev_bearer_headers,
    fanout_send,
    info,
    kv,
    ok,
    out_dir,
    record_fleet_complete,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    warn,
)

FEATURE = "fleet"
DESCRIPTION = "Spawn 9 fleet shell workers, register with the External Management Tier, broadcast a deploy command"
TITLE = "Fleet Management"
SUBTITLE = "Broadcast deploy to 9 workers simultaneously"
HIGHLIGHT_START_S: float = 13.0
HIGHLIGHT_DURATION_S: float = 8.0
# Multi-browser demo: 9 fleet workers each record separately; grid.mp4 is
# the composited 3x3 view used as the catalog video.
PRIMARY_VIDEO: str = "grid_trim.mp4"

SESSION_IDS = [f"fleet-{i}" for i in range(9)]

# These workers are "degraded" — they fail the health check during deploy
FAIL_NODES = {"fleet-2", "fleet-7"}

# One service name per session slot (positional — fleet-N → _SERVICES[N])
_SERVICES = [
    "api-gateway",
    "auth-svc",
    "cache-worker",
    "db-proxy",
    "event-bus",
    "fanout-relay",
    "grpc-router",
    "health-monitor",
    "ingest-api",
]
_IMAGES = [
    "nginx:1.25-alpine",
    "node:20-slim",
    "python:3.12-alpine",
    "golang:1.22-bookworm",
    "redis:7.2-alpine",
    "envoy:v1.29",
    "caddy:2.7-alpine",
    "haproxy:2.9-alpine",
    "traefik:v3.0",
]


def _deploy_cmd(sid: str, rng: _random.Random, fail: bool) -> str:
    """Build a unique multi-step deploy command sequence for one terminal."""
    idx = int(sid.split("-")[1])
    svc = _SERVICES[idx % len(_SERVICES)]
    img = rng.choice(_IMAGES)
    rev = format(rng.getrandbits(28), "07x")
    ms = rng.randint(88, 742)
    n_secrets = rng.randint(2, 8)
    n_migrations = rng.choice([0, 0, 0, 1, 2, 3])

    lines = [
        f"deploy {svc} [rev={rev}]",
        f"  image: {img}",
        f"  secrets: {n_secrets} injected",
        f"  migrations: {n_migrations} applied",
        "  health: CRITICAL — port unreachable" if fail else f"  health: ok ({ms}ms)",
        "✗ aborted" if fail else "✓ ready",
    ]
    # Chain echo commands — simpler than printf, no quoting/escape issues
    cmd = "; ".join(f'echo "{line}"' for line in lines)
    return f"{cmd}\r"


async def run_terminal_demo() -> None:
    """Run the fleet feature demo."""
    banner(DESCRIPTION)
    base_url, server = start_server()
    time.sleep(1.5)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        info(f"Starting {len(SESSION_IDS)} fleet PTY worker sessions...")
        for sid in SESSION_IDS:
            r = await client.post(
                "/api/sessions",
                json={
                    "session_id": sid,
                    "connector_type": "pty",
                    "auto_start": True,
                    "connector_config": {"command": "/bin/bash"},
                },
            )
            r.raise_for_status()
            ok(f"{sid} ready")

        await asyncio.sleep(1.5)

        info("Registering fleet with fanout group...")
        r = await client.post(
            "/api/fanout/groups",
            json={"name": "fleet-demo", "worker_ids": SESSION_IDS},
        )
        r.raise_for_status()
        group_data = r.json()
        group_id = group_data["group_id"]
        kv("group_id", group_id[:12] + "...")
        kv("workers", group_data.get("session_count", len(SESSION_IDS)))

        info("Broadcasting deploy trigger to all fleet workers...")
        r = await client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={"data": "printf 'deploy v2.1 broadcast received\\n'\r", "quiesce_ms": 1000, "max_response_ms": 8000},
        )
        r.raise_for_status()

        rng = _random.Random(42)
        info("Sending per-worker deploy output...")
        for sid in SESSION_IDS:
            cmd = _deploy_cmd(sid, rng, sid in FAIL_NODES)
            send_to_session(base_url, sid, cmd, wait_s=0.2)
            if sid in FAIL_NODES:
                warn(f"  {sid}: ✗ aborted (health: CRITICAL)")
            else:
                ok(f"  {sid}: ✓ ready")

        ok(f"Fleet deploy complete — {len(SESSION_IDS) - len(FAIL_NODES)} ready, {len(FAIL_NODES)} need attention")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + per-worker mp4s + combined 3x3 grid mp4."""
    feat_dir = out_dir(FEATURE, base_out)
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    base_url, server = start_server()
    time.sleep(1.5)

    group_id = ""
    session_groups: dict[str, str] = {}  # sid → single-session fanout group_id
    try:
        with httpx.Client(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
            for sid in SESSION_IDS:
                client.post(
                    "/api/sessions",
                    json={
                        "session_id": sid,
                        "connector_type": "pty",
                        "auto_start": True,
                        "connector_config": {"command": "/bin/bash"},
                    },
                )
            time.sleep(2.0)
            r = client.post(
                "/api/fanout/groups",
                json={"name": "fleet-demo", "worker_ids": SESSION_IDS},
            )
            if r.status_code == 200:
                group_id = r.json().get("group_id", "")
            # Pre-create single-session groups for per-terminal unique output
            for sid in SESSION_IDS:
                r2 = client.post("/api/fanout/groups", json={"name": f"solo-{sid}", "worker_ids": [sid]})
                if r2.status_code == 200:
                    session_groups[sid] = r2.json().get("group_id", "")
    except Exception as exc:
        print(f"  [WARN] setup failed: {exc}", flush=True)

    for sid in SESSION_IDS:
        send_to_session(base_url, sid, "clear\r", wait_s=0.1)
        label = "DEGRADED — awaiting deploy" if sid in FAIL_NODES else "idle — awaiting deploy broadcast"
        send_to_session(base_url, sid, f"echo '{sid}: {label}'\r", wait_s=0.3)

    def do_broadcast(page: object) -> None:
        if not group_id:
            return
        rng = _random.Random(42)

        # Fanout: broadcast deploy trigger to all 9 simultaneously
        fanout_send(base_url, group_id, "printf 'deploy v2.1 broadcast received\\n'\r", wait_s=0.8)

        # Per-session: send unique deploy output via pre-created single-session groups
        # (uses fanout path → direct to PTY worker WebSocket, not hijack)
        for sid in SESSION_IDS:
            gid = session_groups.get(sid)
            if gid:
                cmd = _deploy_cmd(sid, rng, sid in FAIL_NODES)
                fanout_send(base_url, gid, cmd, wait_s=0.1)

        # Populate results panel: 7 green (✓ ready), 2 orange (✗ aborted)
        majority = "✓ ready"
        results = [{"worker_id": sid, "output": "✗ aborted" if sid in FAIL_NODES else "✓ ready"} for sid in SESSION_IDS]
        stmts = "\n".join(
            f"var el{i} = document.getElementById('rc{i}');"
            f"if (el{i}) {{ el{i}.querySelector('.rout').textContent = {r['output']!r};"
            + (f"  el{i}.querySelector('.rout').classList.add('differ');" if r["output"] != majority else "")
            + "}"
            for i, r in enumerate(results)
        )
        with contextlib.suppress(Exception):
            page.evaluate(f"(function(){{{stmts}}})()")  # type: ignore[union-attr]

    vids = record_fleet_complete(
        base_url,
        SESSION_IDS,
        feat_dir,
        broadcast_fn=do_broadcast,
        settle_s=3.0,
    )
    stop_server(server)

    grid_mp4 = vids.get("grid")
    highlight = trim_clip(grid_mp4, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "highlight": highlight, **vids}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nFleet demo: {result}")
