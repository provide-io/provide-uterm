# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Colima PAM end-to-end smoke test.

Proves that pam_uterm.so fires real PAM events into run_pam_integration()
(the production integration path) when a user SSHes into (and exits from)
the Colima VM.

Unix domain sockets can't cross the virtiofs boundary (EOPNOTSUPP), so the
integration runs inside Colima using the virtiofs-mounted venv Python, and
writes session lifecycle events to a JSON file on the shared filesystem for
the Mac to read.

Usage (from repo root, Colima running with pam_uterm.so installed):
    uv run python scripts/colima_smoke_test.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

COLIMA_PYTHON = "/usr/bin/python3"  # system Python inside Colima VM (Linux)
NOTIFY_SOCKET = Path("/tmp/uterm-notify.sock")
EVENT_LOG = Path.home() / ".colima" / "uterm-smoke-events.json"  # virtiofs shared

# Listener script that runs inside Colima using run_pam_integration().
# Uses a RecordingRegistry that captures session_created / session_stopped
# events to the shared JSON file so the Mac can observe them.
# PamNotifyListener.start is monkey-patched to touch a ready flag once the
# socket is bound (run_pam_integration doesn't expose a readiness hook).
LISTENER_SCRIPT = """\
import asyncio, contextlib, json, os
from pathlib import Path
from provide.terminal.server.models import PamConfig, ServerConfig
from provide.terminal.server.pam_integration import run_pam_integration
from provide.terminal.pty.pam_listener import PamNotifyListener

SOCKET = "{socket}"
LOG = "{log}"
READY = LOG + ".ready"

events = []

def _save():
    Path(LOG).write_text(json.dumps(events))

class _RecordingRuntime:
    async def stop(self):
        events.append({{"type": "session_stopped"}})
        _save()
        print("[pam] session_stopped", flush=True)

class _RecordingRegistry:
    def __init__(self):
        self._runtimes = {{}}

    async def create_session(self, payload):
        sid = str(payload.get("session_id", ""))
        events.append({{
            "type": "session_created",
            "session_id": sid,
            "connector_type": str(payload.get("connector_type", "")),
        }})
        self._runtimes[sid] = _RecordingRuntime()
        _save()
        print(f"[pam] session_created session_id={{sid}}", flush=True)

# Monkey-patch PamNotifyListener.start to signal readiness after socket is bound.
# run_pam_integration() calls listener.start() internally; we hook that call to
# write the .ready flag so the Mac knows it's safe to SSH in.
_orig_start = PamNotifyListener.start
async def _patched_start(self, handler):
    await _orig_start(self, handler)
    Path(READY).touch()
    print("[pam] listener ready at " + self.socket_path, flush=True)
PamNotifyListener.start = _patched_start

async def main():
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    config = ServerConfig(pam=PamConfig(
        notify_socket=SOCKET,
        auto_session=True,
        auto_session_command="/bin/echo",
    ))
    registry = _RecordingRegistry()
    task = asyncio.create_task(run_pam_integration(config, registry))
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

asyncio.run(main())
"""


def _colima(cmd: str, *, capture: bool = False, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["colima", "ssh", "--", "sh", "-c", cmd],
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def main() -> None:
    # Pre-flight: verify pam_uterm.so is wired
    r = _colima("grep pam_uterm /etc/pam.d/sshd 2>/dev/null || echo MISSING", capture=True)
    pam_line = r.stdout.strip()
    if "MISSING" in pam_line or not pam_line:
        print("ERROR: pam_uterm.so not in /etc/pam.d/sshd inside Colima")
        print("Run: bash scripts/colima_install_uterm.sh")
        sys.exit(1)
    print(f"PAM line: {pam_line}")

    # Pre-flight: verify provide-terminal is importable inside Colima
    r2 = _colima(
        "python3 -c 'from provide.terminal.server.pam_integration import run_pam_integration; print(\"ok\")' 2>&1",
        capture=True,
    )
    if "ok" not in r2.stdout:
        print("ERROR: provide-terminal not importable inside Colima")
        print(f"  {r2.stdout.strip()}")
        print("Run: bash scripts/colima_install_uterm.sh")
        sys.exit(1)
    print("provide-terminal importable inside Colima: ok")

    # Clean up any stale files
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ready_flag = Path(str(EVENT_LOG) + ".ready")
    for f in (EVENT_LOG, ready_flag):
        if f.exists():
            f.unlink()

    # Write the listener script to the shared filesystem
    listener_src = LISTENER_SCRIPT.format(
        socket=str(NOTIFY_SOCKET),
        log=str(EVENT_LOG),
    )
    listener_file = Path.home() / ".colima" / "uterm_listener.py"
    listener_file.write_text(listener_src)

    print("Starting run_pam_integration() inside Colima...")
    listener_proc = subprocess.Popen(
        ["colima", "ssh", "--", "sh", "-c", f"{COLIMA_PYTHON} {listener_file}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for listener to signal readiness (creates .ready flag file)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready_flag.exists():
            break
        time.sleep(0.1)
    else:
        print("ERROR: Listener did not start within 15 seconds")
        listener_proc.terminate()
        sys.exit(1)

    print("Listener ready. SSHing into Colima (triggers PAM open + close)...")

    # Use direct SSH with ControlMaster=no so sshd creates a fresh PAM session.
    # colima ssh reuses an existing ControlMaster connection which bypasses PAM.
    ssh_cfg = Path.home() / ".colima" / "ssh_config"
    ssh_result = subprocess.run(
        [
            "ssh",
            "-F",
            str(ssh_cfg),
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-tt",
            "colima",
            "echo pam_smoke_ok && exit",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if ssh_result.returncode != 0:
        print(f"ERROR: SSH failed: {ssh_result.stderr}")
        listener_proc.terminate()
        sys.exit(1)
    print(f"SSH output: {ssh_result.stdout.strip()}")

    # Wait for session_stopped event (PAM fires close after session ends;
    # _on_close calls runtime.stop() which RecordingRuntime records)
    deadline = time.monotonic() + 10
    events: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        if EVENT_LOG.exists():
            try:
                events = json.loads(EVENT_LOG.read_text())
                if any(e["type"] == "session_stopped" for e in events):
                    break
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.2)

    listener_proc.terminate()
    try:
        listener_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        listener_proc.kill()

    # Cleanup
    for f in (EVENT_LOG, ready_flag, listener_file):
        if f.exists():
            f.unlink()

    # --- Report ---
    print()
    print("=" * 60)
    print(f"Events received: {len(events)}")
    for ev in events:
        if ev["type"] == "session_created":
            print(f"  session_created  session_id={ev['session_id']}  connector={ev['connector_type']}")
        elif ev["type"] == "session_stopped":
            print("  session_stopped")
    print("=" * 60)

    errors: list[str] = []
    created = [e for e in events if e["type"] == "session_created"]
    stopped = [e for e in events if e["type"] == "session_stopped"]

    if not created:
        errors.append("FAIL: no 'session_created' event received (PAM open not dispatched)")
    else:
        ev = created[0]
        if not ev.get("session_id", "").startswith("pam-"):
            errors.append(f"FAIL: unexpected session_id={ev.get('session_id')}")
        if ev.get("connector_type") != "pty":
            errors.append(f"FAIL: expected connector_type=pty, got {ev.get('connector_type')}")

    if not stopped:
        errors.append("FAIL: no 'session_stopped' event received (PAM close not dispatched)")

    print()
    if errors:
        for err in errors:
            print(err)
        sys.exit(1)
    else:
        print("PASS: session_created + session_stopped events received via run_pam_integration()")
        print(f"      session_id={created[0]['session_id']}")


if __name__ == "__main__":
    main()
