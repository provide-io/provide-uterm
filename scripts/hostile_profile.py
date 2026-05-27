#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import websockets


@dataclass(slots=True)
class ProbeSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0


async def _health(base_url: str, timeout_s: float) -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            resp = await client.get("/api/health")
            return resp.status_code == 200
    except Exception:
        return False


def _ws_url(base_url: str, worker_id: str) -> str:
    proto = "wss://" if base_url.startswith("https://") else "ws://"
    host = base_url.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"{proto}{host}/ws/browser/{worker_id}/term"


async def _slowloris_once(base_url: str, header_bytes_per_chunk: int, delay_s: float, timeout_s: float) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return False  # raw TLS slowloris is intentionally out of scope for this helper

    _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    try:
        req = (
            "GET /ws/browser/provide-shell/term HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "\r\n"
        ).encode("ascii")
        for i in range(0, len(req), header_bytes_per_chunk):
            writer.write(req[i : i + header_bytes_per_chunk])
            await writer.drain()
            await asyncio.sleep(delay_s)
        # If we reach here without reset/timeout, treat as "accepted slow drip".
        return True
    except Exception:
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _oversized_ws_frame_once(ws_url: str, payload_bytes: int, timeout_s: float) -> bool:
    giant = "A" * payload_bytes
    msg = {"type": "input", "data": giant}
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            await ws.send(json.dumps(msg))
            # The server may close, emit error, or ignore. Any non-crash behavior counts as success.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            return True
    except Exception:
        return True


async def _burst_ws_once(ws_url: str, timeout_s: float) -> bool:
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            await ws.send(json.dumps({"type": "input", "data": "echo burst\n"}))
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            return True
    except Exception:
        return False


async def run(args: argparse.Namespace) -> int:
    before = await _health(args.base_url, args.timeout_s)
    if not before:
        print("health check failed before probes")
        return 2

    ws_url = _ws_url(args.base_url, args.worker_id)
    summary = ProbeSummary()
    started = time.perf_counter()

    if args.mode == "slowloris":
        coros = [
            _slowloris_once(args.base_url, args.header_bytes_per_chunk, args.delay_s, args.timeout_s)
            for _ in range(args.iterations)
        ]
    elif args.mode == "oversized":
        coros = [_oversized_ws_frame_once(ws_url, args.payload_bytes, args.timeout_s) for _ in range(args.iterations)]
    else:
        coros = [_burst_ws_once(ws_url, args.timeout_s) for _ in range(args.iterations)]

    sem = asyncio.Semaphore(args.concurrency)

    async def _bounded(coro):
        async with sem:
            return await coro

    for ok in await asyncio.gather(*[_bounded(c) for c in coros], return_exceptions=True):
        summary.attempted += 1
        if ok is True:
            summary.succeeded += 1
        else:
            summary.failed += 1

    after = await _health(args.base_url, args.timeout_s)
    duration_s = time.perf_counter() - started
    print(
        f"mode={args.mode} attempted={summary.attempted} succeeded={summary.succeeded} "
        f"failed={summary.failed} duration_s={duration_s:.2f} healthy_after={after}"
    )
    failure_rate = (summary.failed / summary.attempted) if summary.attempted else 1.0
    success_rate = (summary.succeeded / summary.attempted) if summary.attempted else 0.0
    print(
        f"success_rate={success_rate:.4f} failure_rate={failure_rate:.4f} "
        f"min_success_rate={args.min_success_rate:.4f} max_failure_rate={args.max_failure_rate:.4f}"
    )
    if not after:
        print("health check failed after probes")
        return 1
    if success_rate < args.min_success_rate:
        print("success rate below threshold")
        return 1
    if failure_rate > args.max_failure_rate:
        print("failure rate above threshold")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hostile-client probe for uterm server.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8400")
    parser.add_argument("--worker-id", default="provide-shell", help="Session/worker ID")
    parser.add_argument("--mode", choices=("slowloris", "oversized", "burst"), default="oversized")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--payload-bytes", type=int, default=2_000_000, help="oversized mode payload size")
    parser.add_argument("--header-bytes-per-chunk", type=int, default=8, help="slowloris chunk size")
    parser.add_argument("--delay-s", type=float, default=0.15, help="slowloris delay between chunks")
    parser.add_argument("--min-success-rate", type=float, default=0.5)
    parser.add_argument("--max-failure-rate", type=float, default=0.5)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
