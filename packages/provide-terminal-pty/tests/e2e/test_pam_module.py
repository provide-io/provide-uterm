# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E2E tests for pam_uterm.so — the C PAM session module.

These tests verify the full C→Python chain:
  PamSession.open_session() → libpam → pam_uterm.so → Unix socket → PamNotifyListener

Requirements (enforced via markers):
  - requires_root   — to write /etc/pam.d/ and copy .so into system path
  - pam_uterm.so must be built at ../../native/pam_uterm/pam_uterm.so
  - testuser:testpass123 OS account must exist

The tests create a temporary PAM service file (e.g. /etc/pam.d/uterm-e2e-NNN)
so they never disturb the real sshd/login PAM stack.  Cleanup always runs in
finally blocks.

Note: libpam is loaded with RTLD_GLOBAL by pam.py, so pam_uterm.so can
resolve pam_get_user() symbols without any additional preload step here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from provide.terminal.pty.pam import PamError, PamSession
from provide.terminal.pty.pam_listener import PamEvent, PamNotifyListener

pytestmark = pytest.mark.requires_root

_SO_SRC = Path(__file__).parent.parent.parent / "native" / "pam_uterm" / "pam_uterm.so"

_TEST_USER = "testuser"
_TEST_PASS = "testpass123"  # noqa: S105 — test credential, not a real secret


def _find_pam_module_dir() -> Path:
    """Return the directory where PAM security modules live on this system."""
    candidates = [
        Path("/usr/lib/x86_64-linux-gnu/security"),
        Path("/usr/lib/aarch64-linux-gnu/security"),
        Path("/usr/lib/security"),
        Path("/lib/security"),
        Path("/usr/lib64/security"),
    ]
    for d in candidates:
        if (d / "pam_unix.so").exists():
            return d
    for d in candidates:
        if d.exists():
            return d
    raise RuntimeError(
        "Cannot find PAM module directory (looked for pam_unix.so in standard paths)"
    )


def _install_module() -> Path:
    """Copy pam_uterm.so into the system PAM module directory. Returns dest path."""
    if not _SO_SRC.exists():
        pytest.skip(
            f"pam_uterm.so not built (run: make -C native/pam_uterm/) [{_SO_SRC}]"
        )
    dest = _find_pam_module_dir() / "pam_uterm.so"
    shutil.copy2(_SO_SRC, dest)
    return dest


async def _collect(events: list[PamEvent], ev: PamEvent) -> None:
    events.append(ev)


async def _open_session(service: str) -> PamSession:
    """Authenticate + open a PAM session in a thread (I/O may block briefly)."""
    loop = asyncio.get_event_loop()

    def _run() -> PamSession:
        s = PamSession(service=service)
        s.authenticate(_TEST_USER, _TEST_PASS)
        s.open_session()
        return s

    return await loop.run_in_executor(None, _run)


async def _close_session(session: PamSession) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.close_session)


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_pam_module_open_sends_event() -> None:
    """
    pam_uterm.so fires an 'open' JSON event when PAM open_session is called.

    Chain: PamSession → libpam (RTLD_GLOBAL) → pam_uterm.so → socket → PamNotifyListener
    """
    dest_so = _install_module()
    svc_path: Path | None = None
    session: PamSession | None = None

    with tempfile.TemporaryDirectory() as td:
        sock = str(Path(td) / "notify.sock")

        svc_name = f"uterm-e2e-{os.getpid()}"
        svc_path = Path(f"/etc/pam.d/{svc_name}")
        svc_path.write_text(
            "auth     required pam_unix.so\n"
            "account  required pam_unix.so\n"
            "session  required pam_unix.so\n"
            f"session  optional pam_uterm.so socket={sock}\n"
        )

        events: list[PamEvent] = []
        listener = PamNotifyListener(sock)
        await listener.start(lambda e: _collect(events, e))

        try:
            session = await _open_session(svc_name)
            await asyncio.sleep(0.1)

            assert len(events) >= 1, (
                "Expected at least one event — pam_uterm.so may have failed to connect"
            )
            ev = events[0]
            assert ev.event == "open"
            assert ev.username == _TEST_USER
            assert isinstance(ev.pid, int) and ev.pid > 0
        finally:
            try:
                if session:
                    await _close_session(session)
            except PamError:
                pass
            await listener.stop()
            if svc_path and svc_path.exists():
                svc_path.unlink()
            dest_so.unlink(missing_ok=True)


async def test_pam_module_close_sends_event() -> None:
    """pam_uterm.so fires a 'close' JSON event when PAM close_session is called."""
    dest_so = _install_module()
    svc_path: Path | None = None
    session: PamSession | None = None

    with tempfile.TemporaryDirectory() as td:
        sock = str(Path(td) / "notify.sock")

        svc_name = f"uterm-e2e-close-{os.getpid()}"
        svc_path = Path(f"/etc/pam.d/{svc_name}")
        svc_path.write_text(
            "auth     required pam_unix.so\n"
            "account  required pam_unix.so\n"
            "session  required pam_unix.so\n"
            f"session  optional pam_uterm.so socket={sock}\n"
        )

        events: list[PamEvent] = []
        listener = PamNotifyListener(sock)
        await listener.start(lambda e: _collect(events, e))

        try:
            session = await _open_session(svc_name)
            await asyncio.sleep(0.1)
            assert len(events) >= 1
            assert events[0].event == "open"

            await _close_session(session)
            session = None
            await asyncio.sleep(0.1)

            close_events = [e for e in events if e.event == "close"]
            assert len(close_events) >= 1, (
                "Expected a close event from pam_sm_close_session"
            )
            assert close_events[0].username == _TEST_USER
        finally:
            if session:
                try:
                    await _close_session(session)
                except PamError:
                    pass
            await listener.stop()
            if svc_path and svc_path.exists():
                svc_path.unlink()
            dest_so.unlink(missing_ok=True)


async def test_pam_module_unreachable_socket_does_not_fail_session() -> None:
    """pam_uterm.so must return PAM_SUCCESS even when socket is not listening."""
    dest_so = _install_module()
    svc_path: Path | None = None
    session: PamSession | None = None

    try:
        svc_name = f"uterm-e2e-noconn-{os.getpid()}"
        svc_path = Path(f"/etc/pam.d/{svc_name}")
        svc_path.write_text(
            "auth     required pam_unix.so\n"
            "account  required pam_unix.so\n"
            "session  required pam_unix.so\n"
            "session  optional pam_uterm.so socket=/tmp/uterm-nobody-listening.sock\n"
        )

        session = await _open_session(svc_name)
        await _close_session(session)
        session = None
    finally:
        if session:
            try:
                await _close_session(session)
            except PamError:
                pass
        if svc_path and svc_path.exists():
            svc_path.unlink()
        dest_so.unlink(missing_ok=True)


async def test_pam_module_custom_socket_path_arg() -> None:
    """The socket= argument in the PAM config is honoured by pam_uterm.so."""
    dest_so = _install_module()
    svc_path: Path | None = None
    session: PamSession | None = None

    with tempfile.TemporaryDirectory() as td:
        custom_sock = str(Path(td) / "custom-path.sock")

        svc_name = f"uterm-e2e-custom-{os.getpid()}"
        svc_path = Path(f"/etc/pam.d/{svc_name}")
        svc_path.write_text(
            "auth     required pam_unix.so\n"
            "account  required pam_unix.so\n"
            "session  required pam_unix.so\n"
            f"session  optional pam_uterm.so socket={custom_sock}\n"
        )

        events: list[PamEvent] = []
        listener = PamNotifyListener(custom_sock)
        await listener.start(lambda e: _collect(events, e))

        try:
            session = await _open_session(svc_name)
            await asyncio.sleep(0.1)
            assert any(e.username == _TEST_USER for e in events), (
                f"No event received on custom socket {custom_sock!r}"
            )
        finally:
            if session:
                try:
                    await _close_session(session)
                except PamError:
                    pass
            await listener.stop()
            if svc_path and svc_path.exists():
                svc_path.unlink()
            dest_so.unlink(missing_ok=True)


def test_pam_module_so_is_valid_elf() -> None:
    """pam_uterm.so is a valid ELF shared object exporting the required PAM symbols."""
    if not _SO_SRC.exists():
        pytest.skip("pam_uterm.so not built")
    nm = shutil.which("nm") or "nm"
    result = subprocess.run(
        [nm, "-D", str(_SO_SRC)],
        capture_output=True,
        text=True,
        check=True,
    )
    symbols = result.stdout
    assert "pam_sm_open_session" in symbols, "pam_sm_open_session not exported"
    assert "pam_sm_close_session" in symbols, "pam_sm_close_session not exported"
