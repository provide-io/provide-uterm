#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Subprocess server launcher for multi-backend Playwright parity.

Select backend with env ``UTERM_TEST_BACKEND`` = ``python`` | ``go`` | ``csharp``.
Test-only hooks require ``UTERM_TEST_MODE=1`` on the child process (never default).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_DIR = REPO_ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "uterm" / "server" / "frontend"

WORKER_BEARER = "parity-test-worker-token"


@dataclass(frozen=True)
class BackendServer:
    base_url: str
    jwt: str
    token_path: Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_config(port: int) -> Path:
    base = f"http://127.0.0.1:{port}"
    content = f"""
[server]
host = "127.0.0.1"
port = {port}
public_base_url = "{base}"

[auth]
mode = "dev_token"
worker_bearer_token = "{WORKER_BEARER}"

[ui]
app_path = "/app"
assets_path = "/ui"

[[sessions]]
session_id = "demo"
display_name = "Demo"
connector_type = "shell"
visibility = "public"
owner = "dev-user"
"""
    fd, name = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    path = Path(name)
    path.write_text(content, encoding="utf-8")
    return path


def backend_name() -> str:
    return os.environ.get("UTERM_TEST_BACKEND", "python").strip().lower() or "python"


def skip_unless_python_inprocess_surface(*, reason: str | None = None) -> None:
    """Skip go/csharp multi-backend matrix cells for Python-only in-process suites.

    Surfaces that spin their own FastAPI+TermHub (reconnect spinner,
    control-channel decoder) are not Go/C# servers — claiming them under
    UTERM_TEST_BACKEND=go would be an empty pass.

    Terminal-proxy embed (``mount_terminal_ui`` / ``WsTerminalProxy``) is a
    permanent Python-only product surface; Go/C# use ``uterm proxy`` CLI for
    the same WS→telnet wire path (see HANDOFF permanent de-scope).
    """
    import pytest

    name = backend_name()
    if name in ("go", "csharp"):
        pytest.skip(reason or f"python-only in-process playwright surface (UTERM_TEST_BACKEND={name})")


def build_server_cmd(config_path: Path) -> list[str]:
    backend = backend_name()
    frontend = str(FRONTEND_DIR)
    if backend == "go":
        # Must run inside the Go module (go.mod lives under packages/provide-uterm-go).
        # `go run path/to/main.go` from repo root cannot resolve module imports.
        return [
            "go",
            "run",
            "-C",
            str(REPO_ROOT / "packages/provide-uterm-go"),
            "./cmd/uterm",
            "server",
            "--config",
            str(config_path),
            "--frontend-dir",
            frontend,
        ]
    if backend == "csharp":
        return [
            "dotnet",
            "run",
            "--project",
            str(REPO_ROOT / "packages/provide-uterm-csharp/cmd/Uterm/Uterm.csproj"),
            "--",
            "server",
            "--config",
            str(config_path),
        ]
    if backend != "python":
        raise ValueError(f"unknown UTERM_TEST_BACKEND={backend!r}")
    return [
        sys.executable,
        "-m",
        "provide.uterm.server.cli",
        "--config",
        str(config_path),
    ]


def _wait_for_token(path: Path, deadline: float) -> str:
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        time.sleep(0.05)
    raise RuntimeError(f"dev token not written to {path} within deadline")


@contextmanager
def spawn_backend_server() -> Generator[BackendServer, None, None]:
    """Yield a live server with base URL + minted JWT for browser WS auth."""
    port = _free_port()
    config_path = _write_config(port)
    token_path = Path(tempfile.mkstemp(prefix="uterm-dev-token-", suffix=".jwt")[1])
    token_path.write_text("", encoding="utf-8")
    cmd = build_server_cmd(config_path)
    env = os.environ.copy()
    env["UTERM_TEST_MODE"] = "1"
    env["UTERM_DEV_TOKEN_PATH"] = str(token_path)
    env.setdefault("UTERM_HEADER_MODE_ACK", "1")
    # Never use PIPE without a reader: a chatty server fills the OS pipe buffer
    # and the child blocks on write → WebSocket handshakes hang mid-suite (CI).
    err_path = Path(tempfile.mkstemp(prefix="uterm-backend-", suffix=".stderr")[1])
    err_fh = err_path.open("wb")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err_fh,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 45.0
    try:
        while True:
            if time.monotonic() > deadline:
                proc.terminate()
                err_fh.flush()
                err = err_path.read_bytes()[:2000]
                raise RuntimeError(f"backend {backend_name()!r} failed to start within 45s: {err!r}")
            if proc.poll() is not None:
                err_fh.flush()
                err = err_path.read_bytes()[:2000]
                raise RuntimeError(f"backend {backend_name()!r} exited {proc.returncode}: {err!r}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    break
            except OSError:
                time.sleep(0.1)
        jwt = _wait_for_token(token_path, deadline)
        yield BackendServer(base_url=base_url, jwt=jwt, token_path=token_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        try:
            err_fh.close()
        except OSError:
            pass
        for p in (config_path, token_path, err_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
