#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = round((p / 100.0) * (len(values) - 1))
    return sorted(values)[idx]


def _summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: no samples"
    return (
        f"{name}: count={len(values)} mean={statistics.mean(values):.2f}ms "
        f"p95={_percentile(values, 95):.2f}ms p99={_percentile(values, 99):.2f}ms"
    )


async def _wait_connected(client: httpx.AsyncClient, session_id: str, timeout_s: float) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        status = await client.get(f"/api/sessions/{session_id}")
        if status.status_code == 200 and bool(status.json().get("connected")):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"session {session_id} did not reach connected state")


async def run(base_url: str, worker_id: str, rounds: int, timeout_s: float) -> int:
    send_latencies: list[float] = []
    snapshot_latencies: list[float] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
        health = await client.get("/api/health")
        if health.status_code != 200:
            raise RuntimeError("health check failed")

        await client.post(f"/api/sessions/{worker_id}/connect")
        await _wait_connected(client, worker_id, timeout_s=max(timeout_s, 15.0))
        await client.post(f"/api/sessions/{worker_id}/mode", json={"input_mode": "hijack"})

        acquire = await client.post(
            f"/worker/{worker_id}/hijack/acquire", json={"owner": "latency-probe", "lease_s": 60}
        )
        if acquire.status_code != 200:
            raise RuntimeError(f"hijack acquire failed status={acquire.status_code} body={acquire.text}")
        hijack_id = str(acquire.json()["hijack_id"])

        try:
            for i in range(rounds):
                token = f"latency-probe-{i:04d}"
                t0 = time.perf_counter()
                send = await client.post(f"/worker/{worker_id}/hijack/{hijack_id}/send", json={"keys": token})
                if send.status_code != 200:
                    raise RuntimeError(f"send failed status={send.status_code} body={send.text}")
                send_latencies.append((time.perf_counter() - t0) * 1000.0)

                t1 = time.perf_counter()
                snap = await client.get(f"/worker/{worker_id}/hijack/{hijack_id}/snapshot", params={"wait_ms": 1200})
                if snap.status_code != 200:
                    raise RuntimeError(f"snapshot failed status={snap.status_code} body={snap.text}")
                snapshot_latencies.append((time.perf_counter() - t1) * 1000.0)
        finally:
            await client.post(f"/worker/{worker_id}/hijack/{hijack_id}/release")

    print(_summary("command_send_latency", send_latencies))
    print(_summary("snapshot_fetch_latency", snapshot_latencies))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure command and snapshot latency via REST hijack APIs.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--worker-id", default="demo-session", help="Worker/session ID")
    parser.add_argument("--rounds", type=int, default=40, help="Probe rounds")
    parser.add_argument("--timeout-s", type=float, default=10.0, help="HTTP timeout")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.worker_id, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
