#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Broadcast a config command to 9 sessions simultaneously, show per-node output."""

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

FEATURE = "fanout"
DESCRIPTION = "Broadcast a config command to 9 sessions simultaneously, show per-node output"
TITLE = "Fan-out Broadcast"
SUBTITLE = "Send commands to 9 sessions simultaneously"
HIGHLIGHT_START_S: float = 13.0
HIGHLIGHT_DURATION_S: float = 8.0
# Multi-browser demo: 9 sessions each record separately; grid.mp4 is the
# composited 3x3 view used as the catalog video.
PRIMARY_VIDEO: str = "grid_trim.mp4"

SESSION_IDS = [f"fleet-{i}" for i in range(9)]

# These workers are on "staging" environment — they receive a dry-run override
STAGING_NODES = {"fleet-1", "fleet-6"}

# Config key pool — each terminal gets a random sample
_CONFIG_KEYS = [
    "api.timeout_ms",
    "auth.token_exp_s",
    "cache.ttl_s",
    "circuit.threshold",
    "db.pool_size",
    "grpc.deadline_ms",
    "log.sampling_rate",
    "rate.limit_rps",
    "retry.max_attempts",
    "tls.cert_rotation_h",
    "tracing.sample_pct",
    "ws.ping_interval_s",
]
_REV_PREFIX = "a3f2d91"  # same rev shipped to all — only env differs


def _config_push_cmd(sid: str, rng: _random.Random, staging: bool) -> str:
    """Build a unique config-push command sequence for one terminal."""
    n_keys = rng.randint(3, 6)
    keys = rng.sample(_CONFIG_KEYS, n_keys)
    checksum = format(rng.getrandbits(32), "08x")
    ttl = rng.choice([300, 600, 900, 1800, 3600])
    ms = rng.randint(18, 185)

    lines = [
        f"config push [rev={_REV_PREFIX}, chk={checksum}]",
        f"  keys: {n_keys} updated",
    ]
    lines += [f"  + {k}" for k in keys[:3]]
    if n_keys > 3:
        lines.append(f"  ... +{n_keys - 3} more")
    if staging:
        lines += ["  env: staging — dry-run only", "⚠ not applied"]
    else:
        lines += [f"  ttl: {ttl}s | verified ({ms}ms)", "✓ applied"]

    cmd = "; ".join(f'echo "{line}"' for line in lines)
    return f"{cmd}\r"


async def run_terminal_demo() -> None:
    """Run the fanout feature demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as client:
        info(f"Starting {len(SESSION_IDS)} fleet PTY sessions...")
        for sid in SESSION_IDS:
            r = await client.post(
                "/api/sessions",
                json={
                    "session_id": sid,
                    # The "shell" connector emulates server-side and serves a
                    # snapshot, so browsers render its screen. A raw "pty"/bash
                    # session has no server-side snapshot and the browser xterm
                    # never initialises (blank terminal).
                    "connector_type": "shell",
                    "auto_start": True,
                },
            )
            r.raise_for_status()
            ok(f"{sid} created")

        await asyncio.sleep(2.0)

        info("Creating fan-out group demo-fleet...")
        r = await client.post(
            "/api/fanout/groups",
            json={"name": "demo-fleet", "worker_ids": SESSION_IDS},
        )
        r.raise_for_status()
        group_data = r.json()
        group_id = group_data["group_id"]
        kv("group_id", group_id[:12] + "...")
        kv("worker_count", group_data["session_count"])

        info("Broadcasting config push trigger to all workers...")
        r = await client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={
                "data": f"printf 'config push {_REV_PREFIX} broadcast received\\n'\r",
                "quiesce_ms": 1000,
                "max_response_ms": 8000,
            },
        )
        r.raise_for_status()

        rng = _random.Random(7)
        info("Sending per-worker config output...")
        for sid in SESSION_IDS:
            cmd = _config_push_cmd(sid, rng, sid in STAGING_NODES)
            send_to_session(base_url, sid, cmd, wait_s=0.2)
            if sid in STAGING_NODES:
                warn(f"  {sid}: ⚠ staging — dry-run only")
            else:
                ok(f"  {sid}: ✓ applied")

        ok(f"All {len(SESSION_IDS)} workers responded — {len(STAGING_NODES)} on staging env")

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
                        # "shell" emulates server-side + serves a snapshot so the
                        # browser xterm renders; raw "pty"/bash stays blank.
                        "connector_type": "shell",
                        "auto_start": True,
                    },
                )
            time.sleep(2.0)
            r = client.post(
                "/api/fanout/groups",
                json={"name": "demo-fleet", "worker_ids": SESSION_IDS},
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
        label = "staging — awaiting config broadcast" if sid in STAGING_NODES else "idle — awaiting config broadcast"
        send_to_session(base_url, sid, f"echo '{sid}: {label}'\r", wait_s=0.3)

    def do_broadcast(page: object) -> None:
        if not group_id:
            return
        rng = _random.Random(7)

        # Fanout: broadcast config push trigger to all 9 simultaneously
        fanout_send(base_url, group_id, f"printf 'config push {_REV_PREFIX} broadcast received\\n'\r", wait_s=0.8)

        # Per-session: send unique config output via pre-created single-session groups
        for sid in SESSION_IDS:
            gid = session_groups.get(sid)
            if gid:
                cmd = _config_push_cmd(sid, rng, sid in STAGING_NODES)
                fanout_send(base_url, gid, cmd, wait_s=0.1)

        # Populate results panel: 7 green (✓ applied), 2 orange (⚠ staging)
        majority = "✓ applied"
        results = [
            {"worker_id": sid, "output": "⚠ staging" if sid in STAGING_NODES else "✓ applied"} for sid in SESSION_IDS
        ]
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
        print(f"\nFanout demo: {result}")
