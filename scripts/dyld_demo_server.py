#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live DYLD_INSERT_LIBRARIES injection demo.

Unlike capture_demo_server.py (which manually bridges a PTY), this demo
injects libuterm_capture.dylib directly into the target process via
DYLD_INSERT_LIBRARIES.  The library hooks write()/read()/connect() inside
the target and forwards frames to the CaptureConnector socket automatically.

The target process (a non-SIP binary: homebrew bash if available, otherwise
the uv-managed Python interpreter in interactive mode) runs on a real PTY
for line-editing support.  The PTY master is drained to /dev/null so the
process never blocks, while all actual output arrives via the DYLD hooks.

Usage:
    uv run python scripts/dyld_demo_server.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import socket
import struct
import subprocess
import sys
import tempfile
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

_SERVER_PORT = 59099
_SERVER_BASE = f"http://127.0.0.1:{_SERVER_PORT}"
_DEV_TOKEN = "dev-token"  # noqa: S105
_SESSION_ID = "dyld-demo"

# ── helpers ────────────────────────────────────────────────────────────────────


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
        except Exception:  # noqa: S110
            pass
        time.sleep(0.2)
    raise RuntimeError(f"server not ready at {url} within {timeout}s")


def _api_post(path: str, body: dict) -> dict:
    url = f"{_SERVER_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_DEV_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def _find_injectable_binary() -> tuple[str, list[str], str]:
    """Return (binary_path, argv, display_name) for a non-SIP-protected binary.

    Preference order:
    1. Homebrew bash  — real interactive shell, best UX
    2. uv Python      — injectable, runs interactive REPL with readline
    """
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        if Path(candidate).is_file():
            return candidate, [candidate, "--norc", "--noprofile"], "bash (homebrew)"

    # Fall back to the project venv Python interpreter running a minimal REPL
    # implemented entirely in Python so every write() goes through fd 1 — fully
    # captured by the DYLD hook without readline escape-sequence interference.
    python = str(_REPO_ROOT / ".venv/bin/python3")
    if not Path(python).is_file():
        import shutil

        python = shutil.which("python3") or sys.executable
    repl_script = str(_REPO_ROOT / "scripts" / "_dyld_repl.py")
    return python, [python, repl_script], "python3 (DYLD REPL)"


def _start_server() -> uvicorn.Server:
    config = default_server_config()
    config.auth.mode = "dev"  # type: ignore[assignment]
    config.session_idle_timeout_s = 1800  # auto-sweep idle sessions after 30 min
    config.server = ServerBindConfig(
        host="127.0.0.1",
        port=_SERVER_PORT,
        public_base_url=_SERVER_BASE,
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
    return srv


def _pty_drain(master_fd: int, stop: threading.Event) -> None:
    """Drain the PTY master to /dev/null to prevent back-pressure.

    With DYLD injection active, process output arrives via the hooked write() calls.
    PTY ECHO is disabled on the slave, so the master receives only control bytes
    (e.g. signals). We drain those to /dev/null so the process never blocks.
    """
    devnull = os.open("/dev/null", os.O_WRONLY)
    try:
        while not stop.is_set():
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if data:
                os.write(devnull, data)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _stdin_listener(sock_path: str, master_fd_holder: list[int], stop: threading.Event) -> None:
    """Accept browser keystrokes from the CaptureConnector stdin socket and write
    them to the PTY master fd (which becomes stdin for the injected process).

    master_fd_holder is a one-element list so the caller can update the fd in-place
    when the process restarts — no need to restart this thread.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
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
                        os.write(master_fd_holder[0], data)
        except OSError:
            pass
        finally:
            conn.close()
    srv.close()


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    import fcntl
    import termios

    dylib = _REPO_ROOT / "packages/provide-terminal-platform/native/capture/libuterm_capture.dylib"
    if not dylib.exists():
        print(f"\n  ✗ dylib not found: {dylib}")
        print("  Build it first: make -C packages/provide-terminal-platform/native/capture/")
        sys.exit(1)

    binary, argv, display_name = _find_injectable_binary()
    print()
    print("dyld_demo_server.py — DYLD_INSERT_LIBRARIES injection demo")
    print("=" * 60)
    print(f"  Target:  {display_name}")
    print(f"  Binary:  {binary}")
    print(f"  dylib:   {dylib.name}")
    sys.stdout.flush()

    td_obj = tempfile.TemporaryDirectory()
    td = Path(td_obj.name)
    capture_sock = str(td / "cap.sock")
    stdin_sock = str(td / "stdin.sock")

    # Start FastAPI server
    print(f"\nStarting server on port {_SERVER_PORT}...")
    sys.stdout.flush()
    server = _start_server()
    _wait_http(f"{_SERVER_BASE}/api/health", token=_DEV_TOKEN)
    print(f"  ✓ ready at {_SERVER_BASE}")
    sys.stdout.flush()

    # Register pty_capture session (DYLD output arrives via capture socket)
    _api_post(
        "/api/sessions",
        {
            "session_id": _SESSION_ID,
            "display_name": f"DYLD demo: {display_name}",
            "connector_type": "pty_capture",
            "connector_config": {
                "socket_path": capture_sock,
                "stdin_socket_path": stdin_sock,
                "cols": 120,
                "rows": 40,
            },
            "input_mode": "open",
            "auto_start": True,
            "tags": ["demo", "dyld", "inject"],
            "visibility": "operator",
        },
    )
    print(f"  ✓ session '{_SESSION_ID}' created (connector_type=pty_capture)")
    sys.stdout.flush()

    # Wait for CaptureConnector to create the capture socket
    deadline = time.monotonic() + 8.0
    while not Path(capture_sock).exists():
        if time.monotonic() > deadline:
            print("  ! capture socket never appeared — check server logs", file=sys.stderr)
            break
        time.sleep(0.05)

    stop = threading.Event()

    # Open PTY pair — used for terminal control (line editing, signals)
    master_fd, slave_fd = pty.openpty()
    os.set_inheritable(slave_fd, True)

    # Set PTY size to match connector config
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Disable PTY ECHO on the slave so the kernel doesn't echo stdin back to the
    # master.  With DYLD injection active, the process echoes input via write()
    # (captured by the hook); kernel echo would send the same bytes twice.
    attrs = termios.tcgetattr(slave_fd)
    attrs[3] &= ~termios.ECHO  # lflags: clear ECHO
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    # Mutable holder so _stdin_listener can always write to the current master fd
    # even after a process restart (updated in-place; no thread restart needed).
    master_fd_holder = [master_fd]

    # Start stdin listener before the process so it's ready to accept connections
    t_stdin = threading.Thread(target=_stdin_listener, args=(stdin_sock, master_fd_holder, stop), daemon=True)
    t_stdin.start()

    # Inject the process: PTY slave for stdin/stdout/stderr + DYLD hook for capture
    env = {
        **os.environ,
        "DYLD_INSERT_LIBRARIES": str(dylib),
        "UTERM_CAPTURE_SOCKET": capture_sock,
    }
    if display_name.startswith("bash"):
        env["TERM"] = "xterm-256color"
        env["PS1"] = r"\[\033[1;32m\]dyld-demo\[\033[0m\]:\[\033[1;34m\]\w\[\033[0m\]\$ "
    else:
        # dumb terminal: disables readline's escape-sequence cursor movement so that
        # the >>> prompt is written as plain text through write() — fully captured by
        # the DYLD hook and visible in the browser.
        env["TERM"] = "dumb"

    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    print(f"  ✓ {display_name} started (pid={proc.pid}) with DYLD injection")
    sys.stdout.flush()

    # Forward PTY echo → capture socket so typed characters appear in the browser
    t_drain = threading.Thread(target=_pty_drain, args=(master_fd, stop), daemon=True)
    t_drain.start()

    operator_url = f"{_SERVER_BASE}/app/operator/{_SESSION_ID}"
    print()
    print("=" * 60)
    print(f"  Operator URL:  {operator_url}")
    print()
    print("  DYLD hooks are capturing write()/read()/connect() inside")
    print(f"  the {display_name} process and streaming frames to the browser.")
    print("  Ctrl-C to stop.")
    print("=" * 60)
    sys.stdout.flush()

    _IDLE_TIMEOUT_S = 3600  # self-exit after 1 hour idle
    last_activity = time.monotonic()

    try:
        while True:
            if proc.poll() is not None:
                last_activity = time.monotonic()
                print("\n  Process exited — restarting...")
                sys.stdout.flush()
                _api_post(f"/api/sessions/{_SESSION_ID}/clear", {})
                master_fd2, slave_fd2 = pty.openpty()
                os.set_inheritable(slave_fd2, True)
                fcntl.ioctl(slave_fd2, termios.TIOCSWINSZ, winsize)
                # Disable ECHO on the new slave (same reason as initial setup).
                attrs2 = termios.tcgetattr(slave_fd2)
                attrs2[3] &= ~termios.ECHO
                termios.tcsetattr(slave_fd2, termios.TCSANOW, attrs2)
                proc = subprocess.Popen(  # noqa: S603
                    argv,
                    stdin=slave_fd2,
                    stdout=slave_fd2,
                    stderr=slave_fd2,
                    env=env,
                    close_fds=True,
                )
                os.close(slave_fd2)
                stop.set()
                t_drain.join(timeout=2)
                stop.clear()
                master_fd = master_fd2
                master_fd_holder[0] = master_fd  # update stdin listener in-place
                t_drain = threading.Thread(target=_pty_drain, args=(master_fd, stop), daemon=True)
                t_drain.start()
            if time.monotonic() - last_activity > _IDLE_TIMEOUT_S:
                print("\n  Idle timeout (1h) — shutting down.")
                sys.stdout.flush()
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping...")
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        with contextlib.suppress(OSError):
            os.close(master_fd)
        server.should_exit = True
        td_obj.cleanup()
        print("Done.")


if __name__ == "__main__":
    main()
