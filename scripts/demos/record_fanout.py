#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Broadcast a config command to 9 sessions simultaneously, show per-node output."""

from __future__ import annotations

import asyncio
import contextlib
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
    fanout_send_results,
    info,
    kv,
    ok,
    out_dir,
    record_fleet_complete,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
)
from scripts.demos.output import clean_terminal_output

FEATURE = "fanout"
DESCRIPTION = "Broadcast a config command to 9 sessions simultaneously, show per-node output"
TITLE = "Fan-out Broadcast"
SUBTITLE = "Send commands to 9 sessions simultaneously"
HIGHLIGHT_START_S: float = 13.0
HIGHLIGHT_DURATION_S: float = 8.0

SESSION_IDS = [f"fleet-{i}" for i in range(9)]


async def run_terminal_demo() -> None:
    """Run the fanout feature demo."""
    base_url, server = start_server()
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        info(f"Starting {len(SESSION_IDS)} fleet PTY sessions...")
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

        info('Broadcasting: echo "worker-$$: config applied"; uptime | awk ...')
        r = await client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={
                "data": 'echo "worker-$$: config applied"; uptime | awk \'{print "up "$3}\'\r',
                "quiesce_ms": 1500,
                "max_response_ms": 5000,
            },
        )
        r.raise_for_status()
        send_data = r.json()

        info("Per-worker responses (PIDs differ — separate processes):")
        for i, result in enumerate(send_data.get("results", [])):
            output = clean_terminal_output(result.get("output_delta", ""))
            ok(f"  fleet-{i}: {output}")

        ok(f"All {len(SESSION_IDS)} workers responded — fan-out complete")

    stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + per-worker mp4s + combined 3x3 grid mp4."""
    feat_dir = out_dir(FEATURE, base_out)
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    base_url, server = start_server()
    time.sleep(1.5)

    group_id = ""
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
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
                json={"name": "demo-fleet", "worker_ids": SESSION_IDS},
            )
            if r.status_code == 200:
                group_id = r.json().get("group_id", "")
    except Exception as exc:
        print(f"  [WARN] setup failed: {exc}", flush=True)

    for sid in SESSION_IDS:
        send_to_session(
            base_url,
            sid,
            f"echo '{sid}: idle — awaiting fanout broadcast'\r",
            wait_s=0.3,
        )

    def do_broadcast(page: object) -> None:
        if not group_id:
            return
        results = fanout_send_results(
            base_url,
            group_id,
            'echo "worker-$$: config applied"; uptime | awk \'{print "up "$3}\'\r',
            wait_s=2.5,
        )
        if results:
            outputs = [r["output"] for r in results]
            all_same = len(set(outputs)) == 1
            js_calls = "\n".join(
                f"var el = document.getElementById('rc{i}');"
                f"if (el) {{ el.querySelector('.rout').textContent = {r['output']!r};"
                f"  if (!{str(all_same).lower()}) el.querySelector('.rout').classList.add('differ'); }}"
                for i, r in enumerate(results)
            )
            with contextlib.suppress(Exception):
                page.evaluate(js_calls)  # type: ignore[union-attr]

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
