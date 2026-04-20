#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass

import httpx
import websockets


@dataclass(slots=True)
class ProbeResult:
    connect_ms: float
    hello_ms: float
    ok: bool
    error: str | None = None


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    idx = round((p / 100.0) * (len(values) - 1))
    return sorted(values)[idx]


async def _probe_ws(base_url: str, worker_id: str, timeout_s: float) -> ProbeResult:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/browser/{worker_id}/term"
    start = time.perf_counter()
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            connected = time.perf_counter()
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            msg = json.loads(raw)
            if msg.get("type") != "hello":
                return ProbeResult(
                    connect_ms=(connected - start) * 1000.0,
                    hello_ms=(time.perf_counter() - connected) * 1000.0,
                    ok=False,
                    error="first frame was not hello",
                )
            return ProbeResult(
                connect_ms=(connected - start) * 1000.0,
                hello_ms=(time.perf_counter() - connected) * 1000.0,
                ok=True,
            )
    except Exception as exc:
        return ProbeResult(connect_ms=0.0, hello_ms=0.0, ok=False, error=str(exc))


async def _health_check(base_url: str, timeout_s: float) -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            resp = await client.get("/api/health")
            return resp.status_code == 200
    except Exception:
        return False


async def run(base_url: str, worker_id: str, concurrency: int, rounds: int, timeout_s: float) -> int:
    if not await _health_check(base_url, timeout_s):
        print("health check failed")
        return 2

    results: list[ProbeResult] = []
    failures = 0
    for _ in range(rounds):
        batch = await asyncio.gather(*[_probe_ws(base_url, worker_id, timeout_s=timeout_s) for _ in range(concurrency)])
        results.extend(batch)
        failures += sum(1 for r in batch if not r.ok)

    connect_vals = [r.connect_ms for r in results if r.ok]
    hello_vals = [r.hello_ms for r in results if r.ok]
    print(f"probes={len(results)} ok={len(connect_vals)} failed={failures}")
    if not connect_vals:
        print("no successful probes")
        return 1

    print("connect_ms:")
    print(f"  mean={statistics.mean(connect_vals):.2f}")
    print(f"  p95={_percentile(connect_vals, 95):.2f}")
    print(f"  p99={_percentile(connect_vals, 99):.2f}")
    print("hello_ms:")
    print(f"  mean={statistics.mean(hello_vals):.2f}")
    print(f"  p95={_percentile(hello_vals, 95):.2f}")
    print(f"  p99={_percentile(hello_vals, 99):.2f}")

    if failures:
        for sample in [r for r in results if not r.ok][:5]:
            print(f"failure: {sample.error}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Load/churn profile for browser WS hello latency.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--worker-id", default="demo-session", help="Worker/session ID")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent WS probes per round")
    parser.add_argument("--rounds", type=int, default=25, help="Number of rounds")
    parser.add_argument("--timeout-s", type=float, default=5.0, help="Per-probe timeout seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.worker_id, args.concurrency, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
