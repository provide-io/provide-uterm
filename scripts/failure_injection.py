#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def _wait_connected(client: httpx.AsyncClient, session_id: str, timeout_s: float) -> float:
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        resp = await client.get(f"/api/sessions/{session_id}")
        if resp.status_code == 200 and bool(resp.json().get("connected")):
            return (time.perf_counter() - start) * 1000.0
        await asyncio.sleep(0.1)
    raise TimeoutError(f"session {session_id} did not reconnect within {timeout_s}s")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = round((p / 100.0) * (len(values) - 1))
    return sorted(values)[idx]


async def run(base_url: str, session_id: str, rounds: int, timeout_s: float) -> int:
    durations: list[float] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
        health = await client.get("/api/health")
        if health.status_code != 200:
            print("health check failed")
            return 2
        for _ in range(rounds):
            restart = await client.post(f"/api/sessions/{session_id}/restart")
            if restart.status_code != 200:
                print(f"restart failed status={restart.status_code}")
                return 1
            reconnect_ms = await _wait_connected(client, session_id, timeout_s=timeout_s)
            durations.append(reconnect_ms)

    print(f"rounds={rounds}")
    print(f"reconnect_ms mean={statistics.mean(durations):.2f}")
    print(f"reconnect_ms p95={_percentile(durations, 95):.2f}")
    print(f"reconnect_ms p99={_percentile(durations, 99):.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject worker restart failures and measure reconnect latency.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--session-id", default="demo-session", help="Session ID to restart")
    parser.add_argument("--rounds", type=int, default=20, help="Restart rounds")
    parser.add_argument("--timeout-s", type=float, default=10.0, help="Reconnect timeout in seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.session_id, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
