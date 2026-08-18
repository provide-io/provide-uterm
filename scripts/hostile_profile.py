#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hostile-client resilience probe for the uterm server.

Each probe floods one attack vector — connection burst, oversized WS frame,
slowloris header drip — and classifies every attempt into a *survival*
outcome. The suite asserts the server SURVIVES hostile traffic (it stays
healthy and either refuses or bounds every attempt), NOT that hostile
connections succeed.

Against the default fail-closed server every unauthenticated WS connect is
correctly refused at the auth boundary (the browser route's
``Depends(require_authenticated)`` rejects the upgrade pre-accept, surfaced by
websockets as ``InvalidStatus`` HTTP 403). That clean rejection is the
expected, healthy behavior — not a failure. A *completed* unauthenticated
handshake, by contrast, would be an auth bypass, so the auth-gated probes pass
``--require-refused`` to flag it.

The ``availability`` mode goes one step further: it runs a lane of *legitimate
authenticated* browser sessions concurrently with the hostile flood and asserts
those legitimate clients still connect and receive their ``hello`` control frame
within a latency budget. This is the DoS-starvation signal — proving the server
stays *available* to real users while it refuses hostile traffic, not merely
that it does not crash.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx2
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, encode_control_frame

if TYPE_CHECKING:
    from collections.abc import Awaitable

# ---------------------------------------------------------------------------
# Survival outcomes for a single hostile attempt.
# ---------------------------------------------------------------------------
REFUSED = "refused"  # server cleanly declined (auth 401/403, 1008 close, TCP reset)
COMPLETED = "completed"  # handshake fully succeeded (under fail-closed: an auth leak)
HUNG = "hung"  # attempt exceeded the timeout budget (a liveness / DoS signal)
ERROR = "error"  # server error (5xx / 1011) or an unexpected probe failure


async def _health(base_url: str, timeout_s: float) -> bool:
    try:
        async with httpx2.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            resp = await client.get("/api/health")
            return resp.status_code == 200
    except Exception:
        return False


def _ws_url(base_url: str, worker_id: str) -> str:
    proto = "wss://" if base_url.startswith("https://") else "ws://"
    host = base_url.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"{proto}{host}/ws/browser/{worker_id}/term"


def _classify_ws_failure(exc: BaseException) -> str:
    """Map a websockets / asyncio connect failure to a survival outcome."""
    if isinstance(exc, InvalidStatus):
        # Pre-accept handshake rejection — the fail-closed browser-route shape.
        return REFUSED if exc.response.status_code in (401, 403) else ERROR
    if isinstance(exc, ConnectionClosed):
        # Accept-then-close: 1008 (policy / auth) is a clean refusal; else a fault.
        return REFUSED if exc.code == 1008 else ERROR
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return HUNG
    if isinstance(exc, OSError):
        # TCP refused / reset — the server declined the connection, not a crash.
        return REFUSED
    return ERROR


def _read_dev_token(token_path: str | None = None) -> str | None:
    """Read the dev JWT used to authenticate the availability lane.

    Resolves an explicit ``token_path``, else ``$UTERM_DEV_TOKEN_PATH``. Returns
    ``None`` when no path is configured or the file is absent/empty — mirroring
    the server's own ``dev_idp.read_dev_token`` semantics.
    """
    path = token_path or os.environ.get("UTERM_DEV_TOKEN_PATH")
    if not path:
        return None
    try:
        token = Path(path).read_text().strip()
    except OSError:
        return None
    return token or None


async def _await_hello(ws_url: str, token: str, timeout_s: float) -> str:
    """Open an authenticated browser WS and wait for the ``hello`` control frame.

    Returns ``COMPLETED`` once a ``hello`` control frame arrives, or ``ERROR`` if
    the stream closes before one is seen. Connection failures propagate to the
    caller for classification.
    """
    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=timeout_s,
        close_timeout=timeout_s,
    ) as ws:
        decoder = ControlFrameDecoder()
        async for raw in ws:
            # The inline control channel is text framed; coerce binary frames defensively.
            text = raw if isinstance(raw, str) else raw.decode()
            for event in decoder.feed(text):
                if isinstance(event, ControlChunk) and event.control.get("type") == "hello":
                    return COMPLETED
        return ERROR  # stream ended without a hello frame


async def _authenticated_session_once(ws_url: str, token: str, budget_s: float, timeout_s: float) -> tuple[str, float]:
    """Run one *legitimate* authenticated browser session; report (outcome, latency).

    This lane INVERTS the hostile verdict: a real authenticated client SHOULD
    complete. ``COMPLETED`` (received its ``hello`` within ``budget_s``) is the
    success condition; a refusal/hang/error under valid auth means the server
    failed to stay available to legitimate users while under attack.
    """
    start = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(_await_hello(ws_url, token, timeout_s), timeout=budget_s)
    except TimeoutError:
        outcome = HUNG
    except Exception as exc:
        outcome = _classify_ws_failure(exc)
    return outcome, time.perf_counter() - start


async def _burst_ws_once(ws_url: str, timeout_s: float) -> str:
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            await ws.send(encode_control_frame({"type": "input", "data": "echo burst\n"}))
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            return COMPLETED
    except Exception as exc:
        return _classify_ws_failure(exc)


async def _oversized_ws_frame_once(ws_url: str, payload_bytes: int, timeout_s: float) -> str:
    giant = "A" * payload_bytes
    msg = {"type": "input", "data": giant}
    try:
        async with websockets.connect(ws_url, open_timeout=timeout_s, close_timeout=timeout_s) as ws:
            await ws.send(encode_control_frame(msg))
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            # NOTE: under a fail-closed server the connect is refused BEFORE this
            # runs, so the server's max_ws_message_bytes guard is not exercised in
            # that posture (this cell then asserts refusal, like burst). The guard
            # itself is covered by the server unit tests; an authenticated lane
            # could exercise it end-to-end in future.
            return COMPLETED
    except Exception as exc:
        return _classify_ws_failure(exc)


async def _slowloris_once(base_url: str, header_bytes_per_chunk: int, delay_s: float, timeout_s: float) -> str:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return REFUSED  # raw TLS slowloris is intentionally out of scope for this helper

    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    except TimeoutError:
        return HUNG
    except OSError:
        return REFUSED
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
        return COMPLETED  # server tolerated the slow drip without resetting
    except TimeoutError:
        return HUNG
    except OSError:
        return REFUSED  # server reset the slow client — a bounded, healthy defense
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _run_availability(args: argparse.Namespace) -> int:
    """Availability-under-attack lane: legit authenticated clients vs. a hostile flood.

    Runs an unauthenticated burst (which must be cleanly refused) concurrently
    with a lane of authenticated browser sessions (which must each complete within
    the latency budget). PASS requires the server to stay healthy, refuse every
    hostile attempt (no auth bypass), AND serve every legitimate client.
    """
    token = _read_dev_token(args.token_path)
    if not token:
        print("availability mode requires a dev token (set UTERM_DEV_TOKEN_PATH or --token-path)")
        return 2

    ws_url = _ws_url(args.base_url, args.worker_id)
    sem = asyncio.Semaphore(args.concurrency)

    async def _bounded_hostile(coro: Awaitable[str]) -> str:
        async with sem:
            return await coro

    # Hostile lane: a bounded unauthenticated flood (every attempt must be refused).
    hostile_coros = [_bounded_hostile(_burst_ws_once(ws_url, args.timeout_s)) for _ in range(args.iterations)]
    # Availability lane: authenticated sessions run UNBOUNDED so they genuinely
    # compete with the in-flight flood for the server's attention.
    auth_coros = [
        _authenticated_session_once(ws_url, token, args.latency_budget_s, args.timeout_s)
        for _ in range(args.auth_sessions)
    ]

    started = time.perf_counter()
    results = await asyncio.gather(*hostile_coros, *auth_coros, return_exceptions=True)
    duration_s = time.perf_counter() - started

    hostile_results = results[: args.iterations]
    auth_results = results[args.iterations :]

    hostile_outcomes: Counter[str] = Counter()
    for result in hostile_results:
        hostile_outcomes[result if isinstance(result, str) else ERROR] += 1

    auth_outcomes: Counter[str] = Counter()
    latencies: list[float] = []
    for result in auth_results:
        if isinstance(result, tuple):
            outcome, latency = result
            auth_outcomes[outcome] += 1
            if outcome == COMPLETED:
                latencies.append(latency)
        else:
            auth_outcomes[ERROR] += 1

    after = await _health(args.base_url, args.timeout_s)
    max_latency_s = max(latencies) if latencies else None

    # Hostile lane: every unauthenticated attempt must be cleanly refused — no
    # completed handshake (auth bypass), no hang, no error.
    hostile_ok = hostile_outcomes[COMPLETED] == 0 and hostile_outcomes[HUNG] == 0 and hostile_outcomes[ERROR] == 0
    # Availability lane: every authenticated client must complete within budget
    # WHILE the flood is in flight (the DoS-starvation assertion).
    auth_ok = auth_outcomes[COMPLETED] == args.auth_sessions
    survived = bool(after) and hostile_ok and auth_ok
    verdict = "PASS" if survived else "FAIL"

    latency_str = f"{max_latency_s:.2f}" if max_latency_s is not None else "n/a"
    print(
        f"mode=availability hostile_attempted={args.iterations} hostile_refused={hostile_outcomes[REFUSED]} "
        f"hostile_completed={hostile_outcomes[COMPLETED]} hostile_hung={hostile_outcomes[HUNG]} "
        f"hostile_error={hostile_outcomes[ERROR]} auth_sessions={args.auth_sessions} "
        f"auth_completed={auth_outcomes[COMPLETED]} auth_refused={auth_outcomes[REFUSED]} "
        f"auth_hung={auth_outcomes[HUNG]} auth_error={auth_outcomes[ERROR]} max_auth_latency_s={latency_str} "
        f"duration_s={duration_s:.2f} healthy_after={after} -> {verdict}"
    )
    return 0 if survived else 1


async def run(args: argparse.Namespace) -> int:
    before = await _health(args.base_url, args.timeout_s)
    if not before:
        print("health check failed before probes")
        return 2

    if args.mode == "availability":
        return await _run_availability(args)

    ws_url = _ws_url(args.base_url, args.worker_id)
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

    async def _bounded(coro: Awaitable[str]) -> str:
        async with sem:
            return await coro

    outcomes: Counter[str] = Counter()
    for result in await asyncio.gather(*[_bounded(c) for c in coros], return_exceptions=True):
        outcomes[result if isinstance(result, str) else ERROR] += 1

    after = await _health(args.base_url, args.timeout_s)
    duration_s = time.perf_counter() - started
    attempted = sum(outcomes.values())

    # Survival verdict: the server stayed healthy AND no attempt hung or errored.
    # With --require-refused (the auth-gated WS probes) a COMPLETED unauthenticated
    # connect is an auth bypass and also fails; slowloris omits it (a tolerated
    # slow drip is acceptable as long as nothing hangs or crashes).
    survived = after and outcomes[HUNG] == 0 and outcomes[ERROR] == 0
    if args.require_refused:
        survived = survived and outcomes[COMPLETED] == 0
    verdict = "PASS" if survived else "FAIL"

    print(
        f"mode={args.mode} attempted={attempted} refused={outcomes[REFUSED]} "
        f"completed={outcomes[COMPLETED]} hung={outcomes[HUNG]} error={outcomes[ERROR]} "
        f"duration_s={duration_s:.2f} healthy_after={after} require_refused={args.require_refused} "
        f"-> {verdict}"
    )
    return 0 if survived else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hostile-client survival probe for the uterm server.")
    parser.add_argument("--base-url", required=True, help="Server base URL, e.g. http://127.0.0.1:8780")
    parser.add_argument("--worker-id", default="provide-shell", help="Session/worker ID")
    parser.add_argument("--mode", choices=("slowloris", "oversized", "burst", "availability"), default="burst")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--payload-bytes", type=int, default=2_000_000, help="oversized mode payload size")
    parser.add_argument("--header-bytes-per-chunk", type=int, default=8, help="slowloris chunk size")
    parser.add_argument("--delay-s", type=float, default=0.15, help="slowloris delay between chunks")
    parser.add_argument(
        "--require-refused",
        action="store_true",
        help="Fail if any attempt completes a handshake. Set for auth-gated probes: a completed "
        "unauthenticated connect against a fail-closed server is an auth bypass.",
    )
    parser.add_argument(
        "--auth-sessions",
        type=int,
        default=10,
        help="availability mode: number of legitimate authenticated browser sessions run concurrently "
        "with the hostile flood.",
    )
    parser.add_argument(
        "--latency-budget-s",
        type=float,
        default=5.0,
        help="availability mode: max time for an authenticated session to receive its hello frame.",
    )
    parser.add_argument(
        "--token-path",
        default=None,
        help="availability mode: path to the dev JWT used to authenticate the legit lane "
        "(default: $UTERM_DEV_TOKEN_PATH).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
