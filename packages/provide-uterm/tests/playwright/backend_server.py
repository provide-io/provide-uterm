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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_DIR = REPO_ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "uterm" / "server" / "frontend"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_config(port: int) -> Path:
    base = f"http://127.0.0.1:{port}"
    # dev_token is the local-safe default used by interop tests.
    # Fixed worker bearer so multi-backend tests can attach a fake worker.
    content = f"""
[server]
host = "127.0.0.1"
port = {port}
public_base_url = "{base}"

[auth]
mode = "dev_token"
worker_bearer_token = "parity-test-worker-token"

[ui]
app_path = "/app"
assets_path = "/ui"
"""
    fd, name = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    path = Path(name)
    path.write_text(content, encoding="utf-8")
    return path


WORKER_BEARER = "parity-test-worker-token"


def backend_name() -> str:
    return os.environ.get("UTERM_TEST_BACKEND", "python").strip().lower() or "python"


def build_server_cmd(config_path: Path) -> list[str]:
    backend = backend_name()
    frontend = str(FRONTEND_DIR)
    if backend == "go":
        return [
            "go",
            "run",
            str(REPO_ROOT / "packages/provide-uterm-go/cmd/uterm/main.go"),
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
    # Module entrypoint is already the server (no "server" subcommand).
    # Prefer the project venv interpreter when pytest is run under uv.
    return [
        sys.executable,
        "-m",
        "provide.uterm.server.cli",
        "--config",
        str(config_path),
    ]


@contextmanager
def spawn_backend_server() -> Generator[str, None, None]:
    """Yield ``base_url`` for a live server subprocess."""
    port = _free_port()
    config_path = _write_config(port)
    cmd = build_server_cmd(config_path)
    env = os.environ.copy()
    env["UTERM_TEST_MODE"] = "1"
    # Header auth is loopback-only; force acknowledgement for tests.
    env.setdefault("UTERM_HEADER_MODE_ACK", "1")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 45.0
    try:
        while True:
            if time.monotonic() > deadline:
                proc.terminate()
                err = (proc.stderr.read() if proc.stderr else b"")[:2000]
                raise RuntimeError(f"backend {backend_name()!r} failed to start within 45s: {err!r}")
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else b"")[:2000]
                raise RuntimeError(f"backend {backend_name()!r} exited {proc.returncode}: {err!r}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    break
            except OSError:
                time.sleep(0.1)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass
