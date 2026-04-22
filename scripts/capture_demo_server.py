#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live interactive demo: pty_capture session visible in browser.

Runs a real bash shell via a PTY, bridges its I/O through the CaptureConnector
protocol, and creates a pty_capture session in the FastAPI server.  Browser
input is forwarded back to the shell via the stdin_socket_path feature of
CaptureConnector, making the session fully interactive.

Usage:
    uv run python scripts/capture_demo_server.py
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import select
import socket
import struct
import subprocess  # nosec
import sys
import tempfile
import termios
import threading
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "packages/provide-terminal/src"))
sys.path.insert(0, str(_REPO_ROOT / "packages/provide-terminal-platform/src"))

import uvicorn  # noqa: E402
from provide.terminal.server import create_server_app, default_server_config  # noqa: E402
from provide.terminal.server.models import ServerBindConfig  # noqa: E402

_PYWRANGLER_PORT = 8992
_PYWRANGLER_BASE = f"http://127.0.0.1:{_PYWRANGLER_PORT}"
_DEV_TOKEN = "dev-token"  # noqa: S105
_SESSION_ID = "capture-demo"
_SERVER_PORT = 59099  # fixed so public_base_url is derived correctly at construction time


def _wait_http(url: str, timeout: float = 20.0, token: str | None = None) -> None:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers)  # noqa: S310
            with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
                if resp.status < 500:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"server not ready at {url} within {timeout}s")


def _api_post(base: str, path: str, body: dict, token: str) -> dict:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _start_server() -> tuple[uvicorn.Server, str]:
    base_url = f"http://127.0.0.1:{_SERVER_PORT}"
    config = default_server_config()
    config.auth.mode = "dev"  # type: ignore[assignment]
    config.session_idle_timeout_s = 1800  # auto-sweep idle sessions after 30 min
    config.server = ServerBindConfig(
        host="127.0.0.1",
        port=_SERVER_PORT,
        public_base_url=base_url,
    )
    app = create_server_app(config)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_SERVER_PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server failed to start")
        time.sleep(0.05)
    return srv, base_url


def _make_frame(channel: int, data: bytes) -> bytes:
    return struct.pack(">BI", channel, len(data)) + data


def _pty_to_capture(master_fd: int, capture_sock_path: str, stop: threading.Event) -> None:
    """Read PTY master output and write CHANNEL_STDOUT frames to the capture socket."""
    CHANNEL_STDOUT = 0x01
    # Wait for socket to appear
    deadline = time.monotonic() + 10.0
    while not Path(capture_sock_path).exists():
        if time.monotonic() > deadline:
            return
        time.sleep(0.05)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(capture_sock_path)
    try:
        while not stop.is_set():
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if data:
                    s.sendall(_make_frame(CHANNEL_STDOUT, data))
    except OSError:
        pass
    finally:
        s.close()


def _stdin_listener(stdin_sock_path: str, master_fd: int, stop: threading.Event) -> None:
    """Accept connections on stdin_sock_path and forward bytes to PTY master."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(stdin_sock_path)
    srv.listen(5)
    srv.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        try:
            while not stop.is_set():
                r, _, _ = select.select([conn], [], [], 0.1)
                if r:
                    data = conn.recv(4096)
                    if not data:
                        break
                    with contextlib.suppress(OSError):
                        os.write(master_fd, data)
        except OSError:
            pass
        finally:
            conn.close()
    srv.close()


def main() -> None:
    td_obj = tempfile.TemporaryDirectory()
    td = Path(td_obj.name)
    capture_sock = str(td / "cap.sock")
    stdin_sock = str(td / "stdin.sock")

    print()
    print("capture_demo_server.py — interactive PTY capture demo")
    print("=" * 56)
    sys.stdout.flush()

    # Start pywrangler dev (CF DO)
    cf_dir = _REPO_ROOT / "packages" / "provide-terminal-cloudflare"
    print(f"\nStarting CF DO (pywrangler dev) on port {_PYWRANGLER_PORT}...")
    sys.stdout.flush()
    pywrangler_proc = subprocess.Popen(
        [
            "uv",
            "run",
            "pywrangler",
            "dev",
            "--port",
            str(_PYWRANGLER_PORT),
            "--ip",
            "127.0.0.1",
            "--var",
            "ENVIRONMENT:development",
        ],
        cwd=str(cf_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_http(f"{_PYWRANGLER_BASE}/api/health", token=_DEV_TOKEN)
    print(f"  ✓ CF DO ready at {_PYWRANGLER_BASE}")
    sys.stdout.flush()

    # Start FastAPI server
    print(f"\nStarting FastAPI server on port {_SERVER_PORT}...")
    sys.stdout.flush()
    _server, base_url = _start_server()
    _wait_http(f"{base_url}/api/health", token=_DEV_TOKEN)
    print(f"  ✓ FastAPI ready at {base_url}")
    sys.stdout.flush()

    # Create pty_capture session via REST API with stdin_socket_path
    print("\nCreating pty_capture session via API...")
    sys.stdout.flush()
    _api_post(
        base_url,
        "/api/sessions",
        {
            "session_id": _SESSION_ID,
            "display_name": "Capture Demo (interactive PTY)",
            "connector_type": "pty_capture",
            "connector_config": {
                "socket_path": capture_sock,
                "stdin_socket_path": stdin_sock,
                "cols": 160,
                "rows": 40,
            },
            "input_mode": "open",
            "auto_start": True,
            "tags": ["demo", "capture", "pty"],
            "visibility": "operator",
        },
        _DEV_TOKEN,
    )
    print(f"  ✓ session '{_SESSION_ID}' created (connector_type=pty_capture)")
    sys.stdout.flush()

    # Give the session runtime time to start the connector and create the socket
    deadline = time.monotonic() + 8.0
    while not Path(capture_sock).exists():
        if time.monotonic() > deadline:
            print("  ! capture socket never appeared — check server logs", file=sys.stderr)
            break
        time.sleep(0.05)

    # Create stdin socket listener BEFORE the PTY so CaptureConnector can connect
    stop = threading.Event()
    master_fd, slave_fd = pty.openpty()
    os.set_inheritable(slave_fd, True)
    # Set PTY window size to match connector config so bash readline wraps correctly
    _pty_winsize = struct.pack("HHHH", 40, 160, 0, 0)  # rows, cols, xpix, ypix
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, _pty_winsize)

    # Start stdin listener before the shell so it's ready when the connector connects
    t_stdin = threading.Thread(target=_stdin_listener, args=(stdin_sock, master_fd, stop), daemon=True)
    t_stdin.start()

    # Start real bash shell on slave PTY
    env = {
        "TERM": "xterm-256color",
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PS1": r"\[\033[1;32m\]capture-demo\[\033[0m\]:\[\033[1;34m\]\w\[\033[0m\]\$ ",
    }
    shell_proc = subprocess.Popen(
        ["/bin/bash", "--norc", "--noprofile"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    print(f"  ✓ bash shell started (pid={shell_proc.pid})")
    sys.stdout.flush()

    # Bridge PTY stdout → capture socket
    t_pty = threading.Thread(target=_pty_to_capture, args=(master_fd, capture_sock, stop), daemon=True)
    t_pty.start()

    operator_url = f"{base_url}/app/operator/{_SESSION_ID}"
    print()
    print("=" * 56)
    print(f"  Operator URL:  {operator_url}")
    print(f"  CF DO base:    {_PYWRANGLER_BASE}")
    print()
    print("  Open the URL in a browser — the terminal is fully")
    print("  interactive: type commands and see real shell output.")
    print("  Ctrl-C to stop.")
    print("=" * 56)
    sys.stdout.flush()

    idle_timeout_s = 3600  # self-exit after 1 hour idle
    last_activity = time.monotonic()

    try:
        while True:
            if shell_proc.poll() is not None:
                last_activity = time.monotonic()
                print("\n  Shell exited — restarting...")
                sys.stdout.flush()
                master_fd2, slave_fd2 = pty.openpty()
                os.set_inheritable(slave_fd2, True)
                fcntl.ioctl(slave_fd2, termios.TIOCSWINSZ, _pty_winsize)
                shell_proc = subprocess.Popen(
                    ["/bin/bash", "--norc", "--noprofile"],
                    stdin=slave_fd2,
                    stdout=slave_fd2,
                    stderr=slave_fd2,
                    env=env,
                    close_fds=True,
                )
                os.close(slave_fd2)
                # Restart PTY bridge on new master fd
                stop.set()
                t_pty.join(timeout=2)
                stop.clear()
                master_fd = master_fd2
                t_pty = threading.Thread(target=_pty_to_capture, args=(master_fd, capture_sock, stop), daemon=True)
                t_pty.start()
            if time.monotonic() - last_activity > idle_timeout_s:
                print("\n  Idle timeout (1h) — shutting down.")
                sys.stdout.flush()
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping...")
        sys.stdout.flush()
        stop.set()
        shell_proc.terminate()
        try:
            shell_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            shell_proc.kill()
        with contextlib.suppress(OSError):
            os.close(master_fd)
        _server.should_exit = True
        pywrangler_proc.terminate()
        try:
            pywrangler_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pywrangler_proc.kill()
        td_obj.cleanup()
        print("Done.")


if __name__ == "__main__":
    main()
