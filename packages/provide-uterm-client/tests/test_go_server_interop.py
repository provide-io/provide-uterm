#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live Python-client -> Go-server runtime interop proof.

The mirror image of ``packages/provide-uterm-go/interop/interop_test.go`` (which
drives a real Python ``uterm server`` from the Go client). Here a real Go
``uterm server`` binary is built and launched as a subprocess, then driven from
the real Python client library over the actual wire:

  * REST: ``/api/health`` + ``/api/sessions`` via :class:`HijackClient`, plus the
    full operator hijack lease round-trip (input mode -> acquire -> send ->
    snapshot -> release).
  * WebSocket: the inline DLE/STX control channel -- dial the browser control WS,
    read the hello handshake frame, send an input frame, and read the echoed
    terminal-data frames back.

Architectural note: to drive the hub-based hijack/browser machinery from Python
this test attaches a real Python *worker* to the Go hub over
``/ws/worker/<id>/term``, so the round-trip is Python on both sides: Python
worker <-> Go hub <-> Python browser, plus Python REST client <-> Go hub.

The worker is attached to a session this test *creates* (``POST /api/sessions``),
never to the server's own auto-started ``provide-shell``. A hosted session is one
the server runs a worker for itself: the reference's ``HostedSessionRuntime._run``
starts the connector and then dials its own ``/ws/worker/{session_id}/term``, and
the Go server does the same. Attaching a second, external worker to that id is
therefore a two-workers-one-id collision, and the second worker's ``worker_hello``
resets the hub's ``input_mode`` back to ``open`` -- silently undoing an operator's
``hijack`` and making the acquire that follows fail with 409 ``open input mode``.
That is the *reference's* behaviour too (verified live against the Python server),
so it is faithful port behaviour rather than a Go defect. A test-created session
is never auto-started and so never has a server-side worker, which is what makes
the id ours to attach to. It still exists in the registry the moment it is
created, so REST authorization and browser-role resolution resolve it normally.

Skips gracefully (never fails) when the Go toolchain is unavailable or the binary
cannot be built, mirroring the Go-side conformance/interop skip philosophy.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from provide.uterm.client.control_ws import connect_async_ws
from provide.uterm.client.hijack import HijackClient

# This whole module needs a built Go uterm binary; keep it filterable/skippable
# independently of the rest of the Python suite (mirrors the ``real_cf`` pattern).
pytestmark = pytest.mark.go_interop

# The Go server ships this auto-registered reference session in its default
# config; it exists the moment the server reports healthy. The REST listing
# asserts on it, but nothing attaches to it -- the server hosts its own worker
# there (see the module docstring).
HOSTED_SESSION_ID = "provide-shell"

# Substrings in a `go build` / early-exit log that read as "toolchain or deps
# unavailable" (-> skip) rather than "the server is broken" (-> fail).
_MISSING_DEP_NEEDLES = (
    "cannot find module",
    "no required module",
    "module lookup disabled",
    "dial tcp",
    "connection refused",
    "no such host",
    "i/o timeout",
    "TLS handshake",
    "proxy.golang.org",
    "requires go >=",
    "go: downloading",
    "command not found",
)


@dataclass(frozen=True)
class GoServer:
    """A running Go ``uterm server`` subprocess and its access credentials."""

    base_url: str
    ws_base: str
    token: str  # dev_token JWT for REST + browser WS (admin principal)
    worker_token: str  # worker-bearer token for the worker WS


def _repo_root() -> Path | None:
    """Walk up from this file to the monorepo root (holds packages/*-go/go.mod)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "packages" / "provide-uterm-go" / "go.mod").is_file():
            return parent
    return None


def _free_port() -> int:
    """Ask the OS for an ephemeral loopback port, then release it to be bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _looks_like_missing_deps(text: str) -> bool:
    """Report whether *text* reads as a toolchain/network gap worth skipping."""
    return any(needle in text for needle in _MISSING_DEP_NEEDLES)


def _build_go_binary(go_pkg: Path, out: Path) -> None:
    """Build ``./cmd/uterm`` into *out*; skip on a toolchain/network gap, else fail."""
    result = subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/uterm"],
        cwd=str(go_pkg),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log = result.stdout + result.stderr
        if _looks_like_missing_deps(log):
            pytest.skip(f"go build unavailable (toolchain/deps); skipping:\n{log[-800:]}")
        pytest.fail(f"go build ./cmd/uterm failed:\n{log[-1500:]}")


def _tail(path: Path) -> str:
    """Return the last ~1.5KB of the server log for compact failure output."""
    with contextlib.suppress(OSError):
        return path.read_text(errors="replace")[-1500:]
    return "<no server log>"


def _wait_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Poll /api/health until ok, or the process dies (skip on a dep-gap exit)."""
    deadline = time.monotonic() + 45.0
    with httpx2.Client(timeout=3.0) as probe:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log = _tail(log_path)
                if _looks_like_missing_deps(log):
                    pytest.skip(f"Go server deps unavailable (exit {proc.returncode}); skipping:\n{log}")
                pytest.fail(f"Go server exited before healthy (exit {proc.returncode}):\n{log}")
            with contextlib.suppress(httpx2.HTTPError):
                resp = probe.get(f"{base_url}/api/health")
                if resp.status_code == 200 and resp.json().get("ok") is True:
                    return
            time.sleep(0.25)
    pytest.fail(f"Go server never became healthy within timeout:\n{_tail(log_path)}")


def _read_token(token_path: Path) -> str:
    """Return the dev-token JWT the server writes to UTERM_DEV_TOKEN_PATH at start."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            token = token_path.read_text().strip()
            if token:
                return token
        time.sleep(0.2)
    pytest.fail("dev token file was never written by the Go server")


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the process group, wait briefly, then SIGKILL as a fallback."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


@pytest.fixture
def go_server(tmp_path: Path):
    """Build + launch a real Go ``uterm server`` with dev_token auth, then reap it."""
    if shutil.which("go") is None:
        pytest.skip("go toolchain not on PATH; skipping live Go-server interop test")
    root = _repo_root()
    if root is None:
        pytest.skip("monorepo root not found; skipping live Go-server interop test")

    go_pkg = root / "packages" / "provide-uterm-go"
    binary = tmp_path / "uterm"
    _build_go_binary(go_pkg, binary)

    port = _free_port()
    token_path = tmp_path / "dev_token"
    # A test-local worker-bearer token so the Python worker can attach to the hub.
    # Set via config so SetupDevIDP keeps it (instead of minting a random secret we
    # could not read back). No hardcoded ports/URLs -- only this ephemeral secret.
    worker_token = "wk-" + secrets.token_urlsafe(24)  # pragma: allowlist secret
    config_path = tmp_path / "server.toml"
    config_path.write_text(
        f'[auth]\nmode = "dev_token"\nworker_bearer_token = "{worker_token}"\n'  # pragma: allowlist secret
    )
    log_path = tmp_path / "server.log"

    env = {**os.environ, "UTERM_DEV_TOKEN_PATH": str(token_path)}
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                str(binary),
                "server",
                "--config",
                str(config_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(go_pkg),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group for clean group teardown
        )
        try:
            _wait_healthy(proc, f"http://127.0.0.1:{port}", log_path)
            token = _read_token(token_path)
            yield GoServer(
                base_url=f"http://127.0.0.1:{port}",
                ws_base=f"ws://127.0.0.1:{port}",
                token=token,
                worker_token=worker_token,
            )
        finally:
            _terminate(proc)


async def _create_session(client: HijackClient) -> str:
    """Create a fresh, never-auto-started session and return its id.

    This is the session the Python worker attaches to. It must not be one the
    server hosts a worker for itself (see the module docstring), and a session
    created over REST is seeded into the registry without being started, so no
    server-side worker bridge is ever built for it.
    """
    session_id = f"interop-{secrets.token_hex(4)}"
    ok, created = await client.post(
        "/api/sessions",
        json={"session_id": session_id, "connector_type": "shell", "input_mode": "open"},
    )
    assert ok, f"could not create interop session: {created}"
    return session_id


async def _run_echo_worker(ws_base: str, session_id: str, worker_token: str, stop: asyncio.Event) -> None:
    """A minimal Python worker on the Go hub that echoes input + serves snapshots.

    Connects the worker control WS, negotiates protocol via ``worker_hello``, then
    for each inbound input frame appends it to the emulated screen, echoes it back
    as terminal data (so browsers see it), and pushes a fresh-``ts`` snapshot (so
    the REST hijack-snapshot poll sees it). ``snapshot_req`` control frames are
    answered with the current snapshot.
    """
    screen = {"text": ""}
    url = f"{ws_base}/ws/worker/{session_id}/term"
    async with connect_async_ws(
        url,
        role="worker",
        additional_headers={"Authorization": "Bearer " + worker_token},
    ) as ws:
        await ws.send_frame({"type": "worker_hello", "input_mode": "open", "protocol": {"min": 1, "max": 1}})

        async def emit_snapshot() -> None:
            await ws.send_frame(
                {"type": "snapshot", "screen": screen["text"], "cols": 80, "rows": 25, "ts": time.time()}
            )

        await emit_snapshot()
        while not stop.is_set():
            try:
                frame = await asyncio.wait_for(ws.recv_frame(), timeout=0.3)
            except TimeoutError:
                continue
            except Exception:
                return
            kind = frame.get("type")
            if kind == "input":
                screen["text"] += frame.get("data", "")
                await ws.send_frame({"type": "term", "data": frame.get("data", "")})
                await emit_snapshot()
            elif kind == "snapshot_req":
                await emit_snapshot()


async def _wait_worker_registered(client: HijackClient, session_id: str) -> None:
    """Poll until the hub worker is registered (set_input_mode(hijack) succeeds).

    On a session the server does not host, the hub has no worker state for the id
    until our worker's WS registers, so ``set_input_mode`` 404s until then --
    which is what makes this a real synchronisation point rather than a no-op.
    """
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        ok, _ = await client.set_input_mode(session_id, "hijack")
        if ok:
            return
        await asyncio.sleep(0.1)
    pytest.fail("Python worker never registered with the Go hub")


async def _poll_snapshot_for(client: HijackClient, session_id: str, hijack_id: str, marker: str) -> bool:
    """Poll the hijack snapshot until its screen text contains *marker*."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        ok, snap = await client.snapshot(session_id, hijack_id, wait_ms=500)
        if ok and marker in ((snap.get("snapshot") or {}).get("screen", "") or ""):
            return True
        await asyncio.sleep(0.1)
    return False


async def _exercise_browser_ws(server: GoServer, session_id: str, headers: dict[str, str]) -> None:
    """Dial the browser control WS, read the hello, send input, read the echo."""
    url = f"{server.ws_base}/ws/browser/{session_id}/term"
    async with connect_async_ws(url, role="browser", additional_headers=headers) as ws:
        # The browser handshake opens with a hello control frame.
        got_hello = False
        for _ in range(20):
            frame = await asyncio.wait_for(ws.recv_frame(), timeout=5.0)
            if frame.get("type") == "hello":
                got_hello = True
                break
        assert got_hello, "never received hello control frame over the browser WS"

        marker = f"ws-interop-{secrets.token_hex(4)}"
        await ws.send_frame({"type": "input", "data": f"echo {marker}\n"})

        # The echo returns over the live wire as raw terminal-data ("term") frames
        # and/or inside a "snapshot" control frame's screen field. Accept either --
        # both are the Python client decoding real bytes the Go server wrote.
        accumulated = ""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                frame = await asyncio.wait_for(ws.recv_frame(), timeout=1.0)
            except TimeoutError:
                continue
            accumulated += frame.get("data", "") or ""
            accumulated += frame.get("screen", "") or ""
            if marker in accumulated:
                return
        pytest.fail(f"sent marker {marker!r} never echoed back over the browser WS")


async def test_python_client_drives_live_go_server(go_server: GoServer) -> None:
    """Drive a real Go ``uterm server`` from the real Python client over the wire."""
    headers = {"Authorization": "Bearer " + go_server.token}
    async with HijackClient(go_server.base_url, headers=headers) as client:
        # --- REST: authenticated health + session listing -------------------
        ok, health = await client.health()
        assert ok and health.get("status") == "ok", health
        ok, sessions = await client.list_sessions()
        assert ok, sessions
        assert any(s.get("session_id") == HOSTED_SESSION_ID for s in sessions), sessions

        # --- REST: create the session this test owns a worker for -----------
        session_id = await _create_session(client)
        ok, sessions = await client.list_sessions()
        assert ok, sessions
        assert any(s.get("session_id") == session_id for s in sessions), sessions

        # Attach a Python worker to the Go hub for the hijack + browser flows.
        stop = asyncio.Event()
        worker = asyncio.create_task(_run_echo_worker(go_server.ws_base, session_id, go_server.worker_token, stop))
        try:
            await _wait_worker_registered(client, session_id)

            # --- REST: operator hijack lease round-trip ---------------------
            ok, acquire = await client.acquire(session_id, owner="operator")
            assert ok, acquire
            hijack_id = acquire["hijack_id"]
            assert hijack_id, acquire

            marker = f"rest-interop-{secrets.token_hex(4)}"
            ok, _ = await client.send(session_id, hijack_id, keys=f"echo {marker}\n")
            assert ok, "hijack send failed"
            assert await _poll_snapshot_for(client, session_id, hijack_id, marker), (
                f"sent marker {marker!r} never appeared in the hijack snapshot"
            )

            ok, _ = await client.release(session_id, hijack_id)
            assert ok, "hijack release failed"

            # --- WebSocket: inline control-channel input->echo round-trip ---
            # Back to open mode so a browser input frame reaches the worker.
            ok, _ = await client.set_input_mode(session_id, "open")
            assert ok, "set_input_mode(open) failed"
            await _exercise_browser_ws(go_server, session_id, headers)
        finally:
            stop.set()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(worker, timeout=5.0)
