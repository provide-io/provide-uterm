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


def _parse_control_frame(raw: str) -> dict | None:
    """Decode a single control frame from the DLE/STX-framed WS stream.

    The wire format is ``\\x10\\x02 <8-hex-length> : <json>`` (see
    ``provide.uterm.control_channel``); raw terminal bytes are interleaved
    with control frames on the same socket. The probe only cares about the
    first control frame (the ``hello``), so anything that doesn't decode
    into a JSON object returns ``None``.
    """
    if not raw or not raw.startswith("\x10\x02"):
        # Some servers omit the DLE/STX prefix for the very first frame and
        # send the bare ``<len>:<json>`` body. Accept that too.
        head = raw
    else:
        head = raw[2:]
    sep = head.find(":")
    if sep == -1 or sep > 16:
        return None
    try:
        payload = head[sep + 1 :]
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


async def _probe_ws(base_url: str, worker_id: str, timeout_s: float) -> ProbeResult:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/browser/{worker_id}/term"
    start = time.perf_counter()
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            connected = time.perf_counter()
            # The first frame is a hello control frame. The browser channel
            # can interleave snapshots and PTY bytes before/after, so loop
            # until we see hello (or time out).
            deadline = connected + timeout_s
            msg: dict | None = None
            while time.perf_counter() < deadline:
                remaining = max(0.0, deadline - time.perf_counter())
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                if not isinstance(raw, str):
                    continue
                decoded = _parse_control_frame(raw)
                if decoded is not None and decoded.get("type") == "hello":
                    msg = decoded
                    break
            if msg is None:
                return ProbeResult(
                    connect_ms=(connected - start) * 1000.0,
                    hello_ms=(time.perf_counter() - connected) * 1000.0,
                    ok=False,
                    error="no hello frame received before timeout",
                )
            return ProbeResult(
                connect_ms=(connected - start) * 1000.0,
                hello_ms=(time.perf_counter() - connected) * 1000.0,
                ok=True,
            )
    except Exception as exc:  # noqa: BLE001 — probe must report all errors
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
    parser.add_argument("--worker-id", default="provide-shell", help="Worker/session ID")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent WS probes per round")
    parser.add_argument("--rounds", type=int, default=25, help="Number of rounds")
    parser.add_argument("--timeout-s", type=float, default=5.0, help="Per-probe timeout seconds")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.worker_id, args.concurrency, args.rounds, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
