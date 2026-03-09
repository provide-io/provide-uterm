#!/usr/bin/env python3
"""Inject failure scenarios and measure recovery.

Scenarios:
  restart      Worker restart churn; measures reconnect latency (default).
  ws_flap      Browser WS disconnect/reconnect churn; measures hello latency.
  lease_expiry Hijack lease expiry without heartbeat; measures expiry detection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx
import websockets


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = round((p / 100.0) * (len(values) - 1))
    return sorted(values)[idx]


# ---------------------------------------------------------------------------
# Scenario: restart
# ---------------------------------------------------------------------------


async def _wait_connected(client: httpx.AsyncClient, session_id: str, timeout_s: float) -> float:
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        resp = await client.get(f"/api/sessions/{session_id}")
        if resp.status_code == 200 and bool(resp.json().get("connected")):
            return (time.perf_counter() - start) * 1000.0
        await asyncio.sleep(0.1)
    raise TimeoutError(f"session {session_id} did not reconnect within {timeout_s}s")


async def _run_restart(client: httpx.AsyncClient, session_id: str, rounds: int, timeout_s: float) -> int:
    durations: list[float] = []
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


# ---------------------------------------------------------------------------
# Scenario: ws_flap
# ---------------------------------------------------------------------------


async def _ws_hello_ms(ws_url: str, timeout_s: float) -> float:
    """Connect, wait for hello, return time-to-hello in ms."""
    t0 = time.perf_counter()
    async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.01))
            if json.loads(raw).get("type") == "hello":
                return (time.perf_counter() - t0) * 1000.0
    raise TimeoutError("no hello frame received")


async def _run_ws_flap(base_url: str, worker_id: str, rounds: int, timeout_s: float) -> int:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/browser/{worker_id}/term"
    durations: list[float] = []
    errors = 0
    for _ in range(rounds):
        try:
            hello_ms = await _ws_hello_ms(ws_url, timeout_s)
            durations.append(hello_ms)
        except Exception as exc:
            errors += 1
            print(f"ws_flap probe error: {exc}")
    print(f"rounds={rounds} ok={len(durations)} failed={errors}")
    if not durations:
        print("no successful probes")
        return 1
    print(f"hello_ms mean={statistics.mean(durations):.2f}")
    print(f"hello_ms p95={_percentile(durations, 95):.2f}")
    print(f"hello_ms p99={_percentile(durations, 99):.2f}")
    return 0


# ---------------------------------------------------------------------------
# Scenario: lease_expiry
# ---------------------------------------------------------------------------


async def _run_lease_expiry(client: httpx.AsyncClient, worker_id: str, rounds: int, timeout_s: float) -> int:
    """Acquire a short-lived hijack, let it expire without a heartbeat.

    Expiry is detected by polling re-acquire: if a second acquire succeeds (200)
    the slot has been freed, confirming the original lease was reaped.
    """
    lease_s = 5
    detection_latencies: list[float] = []

    for i in range(rounds):
        acquire = await client.post(
            f"/worker/{worker_id}/hijack/acquire",
            json={"owner": f"lease-expiry-probe-{i}", "lease_s": lease_s},
        )
        if acquire.status_code != 200:
            print(f"acquire failed status={acquire.status_code} body={acquire.text}")
            return 1

        # Wait past the lease window without heartbeating, then poll for slot freedom.
        expected_expiry = time.perf_counter() + lease_s
        await asyncio.sleep(lease_s + 0.5)

        deadline = time.perf_counter() + timeout_s
        detected = False
        while time.perf_counter() < deadline:
            probe = await client.post(
                f"/worker/{worker_id}/hijack/acquire",
                json={"owner": f"lease-expiry-reacquire-{i}", "lease_s": 5},
            )
            if probe.status_code == 200:
                # Slot freed: expiry was detected.
                detection_ms = (time.perf_counter() - expected_expiry) * 1000.0
                detection_latencies.append(detection_ms)
                hid = probe.json().get("hijack_id")
                if hid:
                    await client.post(f"/worker/{worker_id}/hijack/{hid}/release")
                detected = True
                break
            await asyncio.sleep(0.1)

        if not detected:
            print(f"round {i}: lease expiry not detected within {timeout_s}s after expected expiry")
            return 1

    print(f"rounds={rounds}")
    print(f"expiry_detection_ms mean={statistics.mean(detection_latencies):.2f}")
    print(f"expiry_detection_ms p95={_percentile(detection_latencies, 95):.2f}")
    print(f"expiry_detection_ms p99={_percentile(detection_latencies, 99):.2f}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run(base_url: str, session_id: str, scenario: str, rounds: int, timeout_s: float) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s + 10) as client:
        health = await client.get("/api/health")
        if health.status_code != 200 and (health.status_code != 404 or scenario not in {"ws_flap", "lease_expiry"}):
            # Demo server has no /api/health; treat 404 as OK for WS-only scenarios.
            print(f"health check failed: status={health.status_code}")
            return 2

        if scenario == "restart":
            return await _run_restart(client, session_id, rounds, timeout_s)
        if scenario == "ws_flap":
            return await _run_ws_flap(base_url, session_id, rounds, timeout_s)
        if scenario == "lease_expiry":
            return await _run_lease_expiry(client, session_id, rounds, timeout_s)

    print(f"unknown scenario: {scenario}")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject failure scenarios and measure recovery.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--session-id", default="demo-session", help="Session/worker ID")
    parser.add_argument(
        "--scenario",
        choices=["restart", "ws_flap", "lease_expiry"],
        default="restart",
        help="Failure scenario to inject (default: restart)",
    )
    parser.add_argument("--rounds", type=int, default=20, help="Injection rounds")
    parser.add_argument("--timeout-s", type=float, default=10.0, help="Per-round timeout in seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.session_id, args.scenario, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
