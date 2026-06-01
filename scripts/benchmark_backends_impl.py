#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Comparative benchmark: FastAPI TermHub vs CF Worker (pywrangler dev).

Runs 4 workloads against both backends and prints a side-by-side table.

Usage:
    uv run python scripts/benchmark_backends.py              # both backends
    uv run python scripts/benchmark_backends.py --fastapi-only
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess  # nosec
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from provide.uterm.control_channel import (
    ControlChannelDecoder,
    ControlChunk,
    encode_control,
)

# ── constants ────────────────────────────────────────────────────────────────

_CF_PORT = 8990
_CF_PACKAGE = Path(__file__).resolve().parent.parent / "packages" / "provide-uterm-cloudflare"

_N_HANDSHAKES = 100
_N_HIJACK_CYCLES = 500
_N_BROADCASTS = 1000
_SCALE_TIERS = [10, 50, 100]


# ── percentile helpers ───────────────────────────────────────────────────────


def _pct(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


# ── backend lifecycle ────────────────────────────────────────────────────────


def _wait_health(base: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


@contextlib.contextmanager
def _fastapi_server():
    """Start an in-process FastAPI TermHub server on a random port."""
    import logging
    import threading

    import uvicorn
    from fastapi import FastAPI

    from provide.uterm.server.bridge.hub.core import TermHub

    logging.getLogger("provide").setLevel(logging.CRITICAL)

    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("FastAPI server failed to start within 10s")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextlib.contextmanager
def _pywrangler_server():
    """Start pywrangler dev as a subprocess."""
    import shutil

    pywrangler = shutil.which("pywrangler") or "pywrangler"
    dev_vars = _CF_PACKAGE / ".dev.vars"
    original = dev_vars.read_text() if dev_vars.exists() else None
    dev_vars.write_text("AUTH_MODE=dev\n")

    proc = subprocess.Popen(
        [pywrangler, "dev", "--port", str(_CF_PORT), "--ip", "127.0.0.1", "--var", "ENVIRONMENT:development"],
        cwd=_CF_PACKAGE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{_CF_PORT}"
    try:
        if not _wait_health(base, timeout=60):
            raise RuntimeError("pywrangler dev failed to start")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if original is None:
            dev_vars.unlink(missing_ok=True)
        else:
            dev_vars.write_text(original)


# ── worker simulation ────────────────────────────────────────────────────────


async def _connect_worker(ws_base: str, worker_id: str):
    """Connect a simulated worker and send initial snapshot. Returns the WS."""
    uri = f"{ws_base}/ws/worker/{worker_id}/term"
    ws = await websockets.connect(uri)
    snapshot = {
        "type": "snapshot",
        "screen": f"bench-{worker_id}",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "bench",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": False,
        "ts": time.time(),
    }
    await ws.send(encode_control(snapshot))
    return ws


async def _connect_browser(ws_base: str, worker_id: str):
    """Connect a browser and wait for hello. Returns (ws, hello_msg)."""
    uri = f"{ws_base}/ws/browser/{worker_id}/term"
    ws = await websockets.connect(uri)
    decoder = ControlChannelDecoder()
    # CF sends hello + hijack_state + snapshot in a burst; drain up to 5 messages
    for _ in range(5):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=3)
        except TimeoutError:
            break
        for chunk in decoder.feed(raw):
            if isinstance(chunk, ControlChunk) and chunk.control.get("type") == "hello":
                return ws, chunk.control
    raise RuntimeError("no hello received")


# ── workload 1: WS handshake ─────────────────────────────────────────────────


async def bench_handshake(base: str, n: int, *, settle_s: float = 0.01) -> list[float]:
    """Measure browser connect + hello latency with a persistent worker.

    *settle_s* controls the delay between browser close and next connect.
    pywrangler dev needs ~0.3s to process DO WS close events; FastAPI needs ~0.01s.
    """
    ws_base = base.replace("http://", "ws://")
    wid = "hs-bench"
    worker_ws = await _connect_worker(ws_base, wid)
    await asyncio.sleep(0.5)  # let worker register in DO

    latencies: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        browser_ws, _hello = await _connect_browser(ws_base, wid)
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000)
        await browser_ws.close()
        await asyncio.sleep(settle_s)

    await worker_ws.close()
    return latencies


# ── workload 2: hijack lifecycle ──────────────────────────────────────────────


def _http_post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        f"{base}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return e.code, {"error": body}


async def bench_hijack(base: str, n: int, *, settle_s: float = 0.0) -> list[float]:
    ws_base = base.replace("http://", "ws://")
    wid = f"hijack-{int(time.time())}"  # unique per run to avoid stale DO state
    worker_ws = await _connect_worker(ws_base, wid)
    await asyncio.sleep(1.0)  # let worker register in DO

    latencies: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()

        status, data = _http_post(base, f"/worker/{wid}/hijack/acquire", {"owner": "bench", "lease_s": 30})
        if status != 200 or not data.get("ok"):
            continue
        hid = data["hijack_id"]

        _http_post(base, f"/worker/{wid}/hijack/{hid}/heartbeat", {"lease_s": 30})

        _http_post(base, f"/worker/{wid}/hijack/{hid}/release", {})

        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000)
        if settle_s:
            await asyncio.sleep(settle_s)

    await worker_ws.close()
    return latencies


# ── workload 3: broadcast throughput ──────────────────────────────────────────


async def bench_broadcast(base: str, n: int) -> tuple[float, list[float]]:
    """Returns (frames_per_sec, per-frame lag list in ms)."""
    ws_base = base.replace("http://", "ws://")
    wid = "bcast-bench"
    worker_ws = await _connect_worker(ws_base, wid)
    browser_ws, _hello = await _connect_browser(ws_base, wid)
    await asyncio.sleep(0.2)

    decoder = ControlChannelDecoder()
    received: list[float] = []

    async def _recv_loop():
        while True:
            try:
                raw = await asyncio.wait_for(browser_ws.recv(), timeout=5)
                ts = time.perf_counter()
                received.extend(
                    ts
                    for chunk in decoder.feed(raw)
                    if isinstance(chunk, ControlChunk) and chunk.control.get("type") == "snapshot"
                )
            except (TimeoutError, websockets.ConnectionClosed):
                break

    recv_task = asyncio.create_task(_recv_loop())

    t_start = time.perf_counter()
    for i in range(n):
        snap = {
            "type": "snapshot",
            "screen": f"frame-{i}",
            "cursor": {"x": 0, "y": 0},
            "cols": 80,
            "rows": 25,
            "screen_hash": f"h{i}",
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": False,
            "ts": time.time(),
        }
        await worker_ws.send(encode_control(snap))

    await asyncio.sleep(2.0)  # drain
    recv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await recv_task

    t_end = time.perf_counter()
    fps = len(received) / (t_end - t_start) if received else 0
    lags = [(received[i] - t_start) * 1000 / (i + 1) for i in range(len(received))] if received else [0]

    await browser_ws.close()
    await worker_ws.close()
    return fps, lags


# ── workload 4: connection scaling ────────────────────────────────────────────


async def bench_scaling(base: str, tiers: list[int], *, settle_s: float = 0.0) -> dict[int, list[float]]:
    ws_base = base.replace("http://", "ws://")
    results: dict[int, list[float]] = {}

    for n_browsers in tiers:
        wid = f"scale-{n_browsers}-{int(time.time())}"
        worker_ws = await _connect_worker(ws_base, wid)
        await asyncio.sleep(0.5)
        browsers: list[Any] = []
        decoders: list[ControlChannelDecoder] = []
        for _ in range(n_browsers):
            bws, _hello = await _connect_browser(ws_base, wid)
            browsers.append(bws)
            decoders.append(ControlChannelDecoder())
            if settle_s:
                await asyncio.sleep(settle_s)
        await asyncio.sleep(0.3)

        # Send 10 snapshots, measure first-browser receive latency
        latencies: list[float] = []
        for i in range(10):
            snap = {
                "type": "snapshot",
                "screen": f"s-{i}",
                "cursor": {"x": 0, "y": 0},
                "cols": 80,
                "rows": 25,
                "screen_hash": f"sc{i}",
                "cursor_at_end": True,
                "has_trailing_space": False,
                "prompt_detected": False,
                "ts": time.time(),
            }
            t0 = time.perf_counter()
            await worker_ws.send(encode_control(snap))

            # Wait for first browser to receive
            try:
                await asyncio.wait_for(browsers[0].recv(), timeout=5)
                t1 = time.perf_counter()
                latencies.append((t1 - t0) * 1000)
            except TimeoutError:
                pass

            # Drain remaining browsers in parallel
            async def _drain(bws: Any) -> None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(bws.recv(), timeout=1)

            await asyncio.gather(*[_drain(browsers[j]) for j in range(1, n_browsers)])

        results[n_browsers] = latencies

        for bws in browsers:
            await bws.close()
        await worker_ws.close()
        await asyncio.sleep(0.2)

    return results


# ── runner ────────────────────────────────────────────────────────────────────


async def _run_all(base: str, label: str, *, is_cf: bool = False) -> dict[str, Any]:
    # pywrangler dev needs longer settle time between WS operations
    settle = 0.3 if is_cf else 0.01
    n_hs = 10 if is_cf else _N_HANDSHAKES  # fewer iterations for CF (DO settle time)
    n_hj = 20 if is_cf else _N_HIJACK_CYCLES
    scale_tiers = [3, 5] if is_cf else _SCALE_TIERS  # pywrangler dev limits DO concurrency

    print(f"\n{'=' * 60}")
    print(f"  {label}: {base}")
    print(f"{'=' * 60}")

    workload_gap = 3.0 if is_cf else 0.1  # pywrangler needs DO settle time

    print(f"  [1/4] WS handshake ({n_hs}x)...", end=" ", flush=True)
    hs = await bench_handshake(base, n_hs, settle_s=settle)
    print(f"p50={_pct(hs, 50):.1f}ms p95={_pct(hs, 95):.1f}ms")
    await asyncio.sleep(workload_gap)

    print(f"  [2/4] Hijack lifecycle ({n_hj}x)...", end=" ", flush=True)
    hj = await bench_hijack(base, n_hj, settle_s=settle)
    hj_ops = len(hj) / (sum(hj) / 1000) if hj else 0
    print(f"p50={_pct(hj, 50):.1f}ms ops/s={hj_ops:.1f}")
    await asyncio.sleep(workload_gap)

    n_bc = 100 if is_cf else _N_BROADCASTS  # fewer broadcasts for CF
    print(f"  [3/4] Broadcast ({n_bc}x)...", end=" ", flush=True)
    fps, lags = await bench_broadcast(base, n_bc)
    print(f"fps={fps:.1f} lag_p95={_pct(lags, 95):.1f}ms")
    await asyncio.sleep(workload_gap)

    print(f"  [4/4] Scaling {scale_tiers}...", end=" ", flush=True)
    sc = await bench_scaling(base, scale_tiers, settle_s=settle)
    for tier, vals in sc.items():
        print(f" @{tier}={_pct(vals, 50):.1f}ms", end="")
    print()

    return {
        "handshake": {"p50": _pct(hs, 50), "p95": _pct(hs, 95), "p99": _pct(hs, 99), "samples": len(hs)},
        "hijack": {
            "p50": _pct(hj, 50),
            "p95": _pct(hj, 95),
            "p99": _pct(hj, 99),
            "ops_per_sec": hj_ops,
            "samples": len(hj),
        },
        "broadcast": {"fps": fps, "lag_p50": _pct(lags, 50), "lag_p95": _pct(lags, 95)},
        "scaling": {str(k): {"p50": _pct(v, 50), "p95": _pct(v, 95)} for k, v in sc.items()},
    }


def _delta(a: float, b: float) -> str:
    if a == 0:
        return "N/A"
    pct = ((b - a) / a) * 100
    return f"{pct:+.0f}%"


def _print_comparison(fa: dict[str, Any], cf: dict[str, Any]) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {'Metric':<25} {'FastAPI':>10} {'CF Worker':>10} {'Delta':>8}")
    print(f"  {'-' * 25} {'-' * 10} {'-' * 10} {'-' * 8}")

    rows = [
        ("WS handshake p50", fa["handshake"]["p50"], cf["handshake"]["p50"], "ms"),
        ("WS handshake p95", fa["handshake"]["p95"], cf["handshake"]["p95"], "ms"),
        ("Hijack cycle p50", fa["hijack"]["p50"], cf["hijack"]["p50"], "ms"),
        ("Hijack ops/sec", fa["hijack"]["ops_per_sec"], cf["hijack"]["ops_per_sec"], ""),
        ("Broadcast fps", fa["broadcast"]["fps"], cf["broadcast"]["fps"], ""),
        ("Broadcast lag p95", fa["broadcast"]["lag_p95"], cf["broadcast"]["lag_p95"], "ms"),
    ]
    all_tiers = sorted(set(fa["scaling"]) | set(cf["scaling"]), key=int)
    for k in all_tiers:
        fa_v = fa["scaling"].get(k, {}).get("p50", 0)
        cf_v = cf["scaling"].get(k, {}).get("p50", 0)
        suffix = ""
        if k not in cf["scaling"]:
            suffix = " (FA only)"
        elif k not in fa["scaling"]:
            suffix = " (CF only)"
        rows.append((f"Scale@{k} p50{suffix}", fa_v, cf_v, "ms"))

    for label, fa_v, cf_v, unit in rows:
        suffix = unit if unit else ""
        print(f"  {label:<25} {fa_v:>9.1f}{suffix} {cf_v:>9.1f}{suffix} {_delta(fa_v, cf_v):>8}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark FastAPI vs CF Worker")
    parser.add_argument("--fastapi-only", action="store_true")
    parser.add_argument("--cf-only", action="store_true")
    args = parser.parse_args()

    results: dict[str, Any] = {}

    if args.fastapi_only:
        with _fastapi_server() as base:
            results["fastapi"] = asyncio.run(_run_all(base, "FastAPI"))
    elif args.cf_only:
        with _pywrangler_server() as base:
            results["cf_worker"] = asyncio.run(_run_all(base, "CF Worker", is_cf=True))
    else:
        # Run each backend in a separate subprocess to avoid event loop contamination
        out_dir = Path("benchmarks")
        out_dir.mkdir(exist_ok=True)

        for flag, key in [("--fastapi-only", "fastapi"), ("--cf-only", "cf_worker")]:
            tmp = out_dir / f"_tmp_{key}.json"
            print(f"\n--- Running {key} benchmark (subprocess) ---")
            rc = subprocess.call(
                [sys.executable, __file__, flag],
                timeout=300,
            )
            if rc == 0 and tmp.parent.joinpath("backend-comparison.json").exists():
                data = json.loads(tmp.parent.joinpath("backend-comparison.json").read_text())
                if key in data:
                    results[key] = data[key]

    if "fastapi" in results and "cf_worker" in results:
        _print_comparison(results["fastapi"], results["cf_worker"])

    # Write JSON
    out_dir = Path("benchmarks")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "backend-comparison.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
