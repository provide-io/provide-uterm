#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared pytest fixtures for provide-uterm tests."""

from __future__ import annotations

import asyncio

# Ensure this repo's src/provide package wins over sibling workspaces on sys.path.
# Skip in mutant context — mutmut's root conftest already prepends mutants/src/
# with trampolined modules; overriding it here would load non-trampolined copies
# and cause all connectors mutants to report no_tests.
import os as _os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn
from fastapi import FastAPI

if not _os.environ.get("MUTANT_UNDER_TEST"):
    _PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
    _PROJECT_SRC_STR = str(_PROJECT_SRC)
    if _PROJECT_SRC_STR in sys.path:
        sys.path.remove(_PROJECT_SRC_STR)
    sys.path.insert(0, _PROJECT_SRC_STR)
    _loaded_provide = sys.modules.get("provide")
    if _loaded_provide is not None:
        loaded_path = str(getattr(_loaded_provide, "__file__", ""))
        if "/provide-uterm/src/provide/" not in loaded_path:
            for name in list(sys.modules):
                if name == "provide" or name.startswith("provide."):
                    del sys.modules[name]

from provide.uterm.server import create_server_app, default_server_config

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def reference_server() -> Generator[str, None, None]:
    """Session-scoped sync fixture: run the hosted reference server app."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    config.recording.enabled_by_default = True
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("reference_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# WorkerController — background worker WS client for Playwright tests
# ---------------------------------------------------------------------------


class WorkerController:
    """Background-thread fake worker WebSocket client for Playwright tests.

    Connects to ``/ws/worker/{worker_id}/term`` on *base_url*, sends an
    initial snapshot, and collects all received messages in ``self.received``.

    Usage::

        ctrl = WorkerController(base_url, worker_id).start()
        # ... run page interactions ...
        msg = ctrl.wait_for(lambda m: m["type"] == "control", timeout=3.0)
        ctrl.stop()
    """

    def __init__(self, base_url: str, worker_id: str) -> None:
        self.received: list[dict[str, Any]] = []
        self._base_url = base_url
        self._worker_id = worker_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> WorkerController:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError(f"WorkerController: worker {self._worker_id!r} did not connect within 5 s")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        finally:
            loop.close()

    async def _connect(self) -> None:
        import websockets

        from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk, encode_control_frame

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        try:
            async with websockets.connect(ws_url) as ws:
                self._connected.set()
                snapshot_msg = {
                    "type": "snapshot",
                    "screen": f"E2E test worker: {self._worker_id}",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": "e2e-hash",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": {"prompt_id": "test_prompt"},
                    "ts": time.time(),
                }
                await ws.send(encode_control_frame(snapshot_msg))
                decoder = ControlFrameDecoder()
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        for chunk in decoder.feed(raw):
                            if isinstance(chunk, ControlChunk):
                                self.received.append(chunk.control)
                            elif isinstance(chunk, DataChunk) and chunk.data:
                                # Hub encodes "input" messages as raw data frames
                                self.received.append({"type": "input", "data": chunk.data})
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()  # unblock callers even on connection failure

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> dict[str, Any] | None:
        """Return the first received message matching *predicate*, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in list(self.received):
                if predicate(msg):
                    return msg
            time.sleep(0.05)
        return None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


class _ThreadedEchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], received_chunks: list[bytes]) -> None:
        self.received_chunks = received_chunks
        super().__init__(server_address, _EchoTelnetHandler)


class _EchoTelnetHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        from provide.uterm.transports.telnet_server import _build_telnet_handshake

        server = self.server
        assert isinstance(server, _ThreadedEchoServer)
        self.request.sendall(_build_telnet_handshake())
        self.request.sendall(b"WELCOME FROM TELNET\r\n")
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            server.received_chunks.append(data)
            self.request.sendall(data)


@pytest.fixture(scope="session")
def terminal_proxy_server() -> Generator[tuple[str, list[bytes]], None, None]:
    """Session-scoped fixture: terminal UI + WS/telnet echo proxy for browser tests."""
    from provide.uterm.fastapi_utils import WsTerminalProxy, mount_terminal_ui

    received_chunks: list[bytes] = []
    telnet_server = _ThreadedEchoServer(("127.0.0.1", 0), received_chunks)
    telnet_thread = threading.Thread(target=telnet_server.serve_forever, daemon=True)
    telnet_thread.start()

    telnet_port = telnet_server.server_address[1]
    app = FastAPI()
    mount_terminal_ui(app)
    app.include_router(WsTerminalProxy("127.0.0.1", telnet_port).create_router("/ws/raw/demo/term"))

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            telnet_server.shutdown()
            telnet_server.server_close()
            raise RuntimeError("terminal_proxy_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", received_chunks

    server.should_exit = True
    thread.join(timeout=5)
    telnet_server.shutdown()
    telnet_server.server_close()
    telnet_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Auto-mark mutation-killing tests so they are excluded from the default run.
# Files matching these patterns are heavy and intended to be run alongside
# mutmut, not as part of normal development test cycles.
# Run them explicitly with: pytest -m mutant
# ---------------------------------------------------------------------------

_MUTANT_FILE_PATTERNS = (
    "mutant",
    "mutation",
    "mutmut",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    mutant_mark = pytest.mark.mutant
    for item in items:
        fspath = str(item.fspath)
        if any(pat in fspath for pat in _MUTANT_FILE_PATTERNS):
            item.add_marker(mutant_mark, append=False)


def pytest_configure(config: pytest.Config) -> None:
    """Initialize the server environment for tests."""
    # Skip under mutmut: this collection-time hook builds a full server app,
    # which instantiates mutated bridge/hub services (e.g. RateLimiter). During
    # the mutmut stats phase the mutated trampoline raises
    # MutmutProgrammaticFailException, aborting the entire stats run before any
    # test binds. The mutmut pytest_add_cli_args_test_selection enumerates its suites explicitly and
    # imports their targets directly, so this connector pre-registration is
    # unnecessary there.
    if _os.environ.get("MUTANT_UNDER_TEST"):
        return
    from provide.uterm.server import create_server_app, default_server_config

    # Ensure default connectors are registered so registry-aware tests (e.g.
    # test_connectors_websocket.py) can find them.
    # Note: create_server_app calls _register_builtin_connectors(config).
    _ = create_server_app(default_server_config(), api_only=True)
