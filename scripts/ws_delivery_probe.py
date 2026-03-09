#!/usr/bin/env python3
"""Measure hub->browser WS broadcast delivery latency.

Strategy: acquire a REST hijack (triggers hub to broadcast a hijack_state
frame to all connected browser WebSockets).  Measure time from the REST
acquire call returning to receiving the hijack_state frame on a concurrently
open browser WS connection.  This exercises the real worker-event->hub->WS
delivery path without requiring a live echo terminal.
"""

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
class DeliveryResult:
    acquire_ms: float
    deliver_ms: float
    ok: bool
    error: str | None = None


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = round((p / 100.0) * (len(values) - 1))
    return sorted(values)[idx]


async def _drain_until(ws: websockets.WebSocketClientProtocol, msg_type: str, timeout: float) -> dict | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            return None
        msg = json.loads(raw)
        if msg.get("type") == msg_type:
            return msg
    return None


async def _probe(base_url: str, worker_id: str, timeout_s: float) -> DeliveryResult:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/browser/{worker_id}/term"
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            hello = await _drain_until(ws, "hello", timeout=timeout_s)
            if hello is None:
                return DeliveryResult(0.0, 0.0, False, "no hello frame received")

            async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as http:
                t0 = time.perf_counter()
                acquire = await http.post(
                    f"/worker/{worker_id}/hijack/acquire",
                    json={"owner": "ws-delivery-probe", "lease_s": 10},
                )
                acquire_ms = (time.perf_counter() - t0) * 1000.0
                if acquire.status_code != 200:
                    return DeliveryResult(acquire_ms, 0.0, False, f"acquire status={acquire.status_code}")
                hijack_id = acquire.json()["hijack_id"]
                try:
                    t1 = time.perf_counter()
                    msg = await _drain_until(ws, "hijack_state", timeout=timeout_s)
                    deliver_ms = (time.perf_counter() - t1) * 1000.0
                    if msg is None:
                        return DeliveryResult(acquire_ms, 0.0, False, "no hijack_state frame received")
                    return DeliveryResult(acquire_ms, deliver_ms, True)
                finally:
                    await http.post(f"/worker/{worker_id}/hijack/{hijack_id}/release")
    except Exception as exc:
        return DeliveryResult(0.0, 0.0, False, str(exc))


async def run(base_url: str, worker_id: str, rounds: int, timeout_s: float) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as http:
        health = await http.get("/api/health")
        if health.status_code not in {200, 404}:
            print(f"server unreachable: status={health.status_code}")
            return 2

    results: list[DeliveryResult] = [await _probe(base_url, worker_id, timeout_s) for _ in range(rounds)]

    ok = [r for r in results if r.ok]
    failures = len(results) - len(ok)
    print(f"rounds={rounds} ok={len(ok)} failed={failures}")
    if not ok:
        print("no successful probes")
        for r in [r for r in results if not r.ok][:5]:
            print(f"  error: {r.error}")
        return 1

    acquire_vals = [r.acquire_ms for r in ok]
    deliver_vals = [r.deliver_ms for r in ok]
    print("acquire_ms (REST hijack acquire round-trip):")
    print(f"  mean={statistics.mean(acquire_vals):.2f}")
    print(f"  p95={_percentile(acquire_vals, 95):.2f}")
    print(f"  p99={_percentile(acquire_vals, 99):.2f}")
    print("ws_deliver_ms (acquire -> browser WS hijack_state frame):")
    print(f"  mean={statistics.mean(deliver_vals):.2f}")
    print(f"  p95={_percentile(deliver_vals, 95):.2f}")
    print(f"  p99={_percentile(deliver_vals, 99):.2f}")
    if failures:
        for r in [r for r in results if not r.ok][:5]:
            print(f"failure: {r.error}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure hub->browser WS broadcast delivery latency via hijack-state frames."
    )
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--worker-id", default="demo-session", help="Worker/session ID")
    parser.add_argument("--rounds", type=int, default=40, help="Probe rounds")
    parser.add_argument("--timeout-s", type=float, default=5.0, help="Per-probe timeout in seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.worker_id, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
