#!/usr/bin/env python3
"""Staging rollback drill.

Simulates a deploy-then-rollback cycle on a live staging server:
  1. Record baseline state (session health + metrics snapshot).
  2. Disrupt: restart the session (analogous to a bad-deploy disruption).
  3. Recover: wait for session to reconnect (analogous to rollback + restart).
  4. Verify: confirm session is healthy and no unexpected metric anomalies.
  5. Output: write a timestamped drill artifact to --out-dir.

Exit codes:
  0  drill passed
  1  drill failed (recovery or verification step)
  2  pre-flight failed (server unreachable or session not found)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


async def _wait_connected(client: httpx.AsyncClient, session_id: str, timeout_s: float) -> float:
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        resp = await client.get(f"/api/sessions/{session_id}")
        if resp.status_code == 200 and bool(resp.json().get("connected")):
            return (time.perf_counter() - start) * 1000.0
        await asyncio.sleep(0.2)
    raise TimeoutError(f"session {session_id} did not reach connected state within {timeout_s}s")


async def run(base_url: str, session_id: str, out_dir: Path, timeout_s: float) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    artifact_path = out_dir / f"rollback-drill-{stamp}.json"

    drill: dict = {
        "drill_timestamp": stamp,
        "base_url": base_url,
        "session_id": session_id,
        "steps": [],
        "passed": False,
    }

    def _step(name: str, result: str, detail: dict | None = None) -> None:
        entry = {"step": name, "result": result}
        if detail:
            entry.update(detail)
        drill["steps"].append(entry)
        status = "PASS" if result == "pass" else "FAIL"
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""))

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
        # --- Pre-flight ---
        health = await client.get("/api/health")
        if health.status_code != 200:
            _step("preflight_health", "fail", {"status": health.status_code})
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 2
        _step("preflight_health", "pass")

        session_resp = await client.get(f"/api/sessions/{session_id}")
        if session_resp.status_code != 200:
            _step("preflight_session_exists", "fail", {"status": session_resp.status_code})
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 2
        _step("preflight_session_exists", "pass")

        # --- Baseline ---
        baseline_metrics = (await client.get("/api/metrics")).json().get("metrics", {})
        baseline_session = session_resp.json()
        _step("baseline_captured", "pass", {"connected": baseline_session.get("connected")})

        # --- Disrupt (simulate bad deploy) ---
        t_disrupt = time.perf_counter()
        restart = await client.post(f"/api/sessions/{session_id}/restart")
        if restart.status_code != 200:
            _step("disrupt_restart", "fail", {"status": restart.status_code})
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 1
        _step("disrupt_restart", "pass")

        # --- Recover (simulate rollback + reconnect) ---
        try:
            reconnect_ms = await _wait_connected(client, session_id, timeout_s=timeout_s)
        except TimeoutError as exc:
            _step("recover_reconnect", "fail", {"error": str(exc)})
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 1
        _step("recover_reconnect", "pass", {"reconnect_ms": round(reconnect_ms, 2)})

        # --- Verify ---
        post_session = (await client.get(f"/api/sessions/{session_id}")).json()
        if not post_session.get("connected"):
            _step("verify_connected", "fail", {"session": post_session})
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 1
        _step("verify_connected", "pass")

        post_metrics = (await client.get("/api/metrics")).json().get("metrics", {})
        delta_5xx = int(post_metrics.get("http_requests_5xx_total", 0)) - int(
            baseline_metrics.get("http_requests_5xx_total", 0)
        )
        _step("verify_no_5xx_spike", "pass" if delta_5xx == 0 else "fail", {"delta_5xx": delta_5xx})
        if delta_5xx > 0:
            artifact_path.write_text(json.dumps(drill, indent=2))
            return 1

        # --- Summary ---
        total_ms = (time.perf_counter() - t_disrupt) * 1000.0
        drill["reconnect_ms"] = round(reconnect_ms, 2)
        drill["total_drill_ms"] = round(total_ms, 2)
        drill["passed"] = True
        artifact_path.write_text(json.dumps(drill, indent=2))
        print(f"\nDrill PASSED. Artifact: {artifact_path}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a staging rollback drill and produce an artifact.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--session-id", default="demo-session", help="Session to use for the drill")
    parser.add_argument("--out-dir", default="artifacts/rollback-drill", help="Output directory for drill artifact")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Reconnect timeout in seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.session_id, Path(args.out_dir), args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
