#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
Proof that libuterm_capture.so intercepts real subprocess I/O via LD_PRELOAD.

Spawns actual subprocesses with LD_PRELOAD=libuterm_capture.so and
UTERM_CAPTURE_SOCKET=<path>, then verifies that CHANNEL_STDOUT / CHANNEL_STDIN
frames arrive on a Unix domain socket.

Only runs on Linux (macOS SIP blocks DYLD_INSERT_LIBRARIES for system binaries,
and the .dylib build is skipped on macOS in CI).

Note: interception is PLT-based, so a command is only captured if it reaches libc
through the dynamic symbol table. read(), write() and splice() are all hooked
(splice via a tee() peek, since it moves bytes in kernel space and issues no
read/write of its own).

What is still NOT captured, by design: a caller that issues the syscall directly
and never binds the libc symbol. Static-linked and vDSO-optimised binaries are the
classic case — /bin/echo on aarch64 glibc — and uutils coreutils' cat is another:
it splices pipe-to-pipe without going through the PLT, so it yields no frames even
though splice is hooked. GNU cat uses read/write and is captured. Do not reach for
/bin/cat as a fixture; which implementation is installed decides the outcome.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from provide.uterm.pty._build import get_capture_lib_path
from provide.uterm.pty.capture import CHANNEL_STDIN, CHANNEL_STDOUT


def _require_linux_and_lib() -> Path:
    """Skip unless on Linux with libuterm_capture.so present."""
    if sys.platform != "linux":
        pytest.skip("LD_PRELOAD capture only supported on Linux (macOS SIP blocks it)")
    lib = get_capture_lib_path()
    if lib is None:
        pytest.skip("libuterm_capture.so not built — run 'make' in native/capture/")
    return lib


def _serve_once(sock_path: str, timeout: float = 3.0) -> bytes:
    """
    Listen on a Unix socket, accept one connection, read all data, return raw bytes.

    Runs synchronously in a thread so it doesn't interfere with the asyncio loop.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(sock_path)
        s.listen(1)
        s.settimeout(timeout)
        conn, _ = s.accept()
        conn.settimeout(timeout)
        chunks: list[bytes] = []
        try:
            while True:
                d = conn.recv(4096)
                if not d:
                    break
                chunks.append(d)
        except OSError:
            pass
        finally:
            conn.close()
        return b"".join(chunks)
    finally:
        s.close()


def _parse_frames(raw: bytes) -> list[tuple[int, bytes]]:
    """Parse wire-format frames: [1B channel][4B length big-endian][N bytes data]."""
    frames: list[tuple[int, bytes]] = []
    i = 0
    while i + 5 <= len(raw):
        channel = raw[i]
        (length,) = struct.unpack(">I", raw[i + 1 : i + 5])
        data = raw[i + 5 : i + 5 + length]
        frames.append((channel, data))
        i += 5 + length
    return frames


def _run_with_capture(
    cmd: list[str],
    lib: Path,
    sock_path: str,
    *,
    stdin: bytes | None = None,
    timeout: float = 5.0,
) -> list[tuple[int, bytes]]:
    """
    Start a socket collector thread, run cmd with LD_PRELOAD, return parsed frames.
    """
    raw_holder: list[bytes] = []
    exc_holder: list[BaseException] = []

    def collect() -> None:
        try:
            raw_holder.append(_serve_once(sock_path, timeout=timeout))
        except Exception as exc:
            exc_holder.append(exc)

    t = threading.Thread(target=collect)
    t.start()

    env = {**os.environ, "LD_PRELOAD": str(lib), "UTERM_CAPTURE_SOCKET": sock_path}
    proc = subprocess.run(
        cmd,
        env=env,
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )
    _ = proc  # returncode not checked — best-effort

    t.join(timeout=timeout + 1)

    if exc_holder:
        pytest.fail(f"socket collector raised: {exc_holder[0]}")

    raw = raw_holder[0] if raw_holder else b""
    return _parse_frames(raw)


# ── tests ─────────────────────────────────────────────────────────────────────


def test_stdout_frames_arrive_from_printf() -> None:
    """LD_PRELOAD intercepts write(1,...) — printf output arrives as CHANNEL_STDOUT."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        frames = _run_with_capture(
            ["/bin/sh", "-c", "printf 'hello-ld-preload\\n'"],
            lib,
            sock_path,
        )

    assert frames, "no frames received — libuterm_capture.so did not connect or send data"
    channels = [ch for ch, _ in frames]
    assert CHANNEL_STDOUT in channels, f"no CHANNEL_STDOUT frame; got channels: {channels}"
    stdout_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDOUT)
    assert b"hello-ld-preload" in stdout_data, f"expected 'hello-ld-preload' in stdout frames, got: {stdout_data!r}"


def test_all_three_words_arrive_in_stdout() -> None:
    """Output from multiple printf args is captured — content check, not frame count."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        frames = _run_with_capture(
            ["/bin/sh", "-c", "printf '%s\\n' first second third"],
            lib,
            sock_path,
        )

    stdout_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDOUT)
    assert b"first" in stdout_data
    assert b"second" in stdout_data
    assert b"third" in stdout_data


def test_no_frames_without_env_var() -> None:
    """Without UTERM_CAPTURE_SOCKET set, the library is inert — no connection made."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        # Remove UTERM_CAPTURE_SOCKET from env; keep LD_PRELOAD
        env_no_socket = {k: v for k, v in os.environ.items() if k != "UTERM_CAPTURE_SOCKET"}
        env_no_socket["LD_PRELOAD"] = str(lib)

        # Start server thread — expect no connection within 0.5s
        raw_holder: list[bytes] = []

        def collect() -> None:
            try:
                raw_holder.append(_serve_once(sock_path, timeout=0.5))
            except OSError:
                raw_holder.append(b"")

        t = threading.Thread(target=collect)
        t.start()

        subprocess.run(
            ["/bin/sh", "-c", "printf 'should-not-be-captured\\n'"],
            env=env_no_socket,
            capture_output=True,
            timeout=5,
        )
        t.join(timeout=2)

    raw = raw_holder[0] if raw_holder else b""
    assert raw == b"", f"expected no data, got: {raw!r}"


def test_stdin_read_produces_channel_stdin_frame() -> None:
    """read() on fd 0 produces a CHANNEL_STDIN frame alongside CHANNEL_STDOUT."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        # The shell's `read` builtin reads fd 0 (CHANNEL_STDIN) and printf writes fd 1
        # (CHANNEL_STDOUT), both through libc. /bin/cat is NOT usable here: uutils
        # coreutils' cat copies pipe-to-pipe with splice(), which moves the bytes in
        # kernel space and issues no read()/write() at all, so nothing is intercepted
        # and the capture socket sees zero frames. GNU cat happens to use read/write,
        # which is why this only fails on a uutils system.
        frames = _run_with_capture(
            ["/bin/sh", "-c", 'read x; printf "%s\\n" "$x"'],
            lib,
            sock_path,
            stdin=b"keystroke-data\n",
        )

    channels = [ch for ch, _ in frames]
    assert CHANNEL_STDIN in channels, f"expected CHANNEL_STDIN frame from the shell; got channels: {channels}"
    stdin_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDIN)
    assert b"keystroke-data" in stdin_data


def test_library_does_not_intercept_non_stdio_fds() -> None:
    """Writes to fd > 2 (capture socket itself) are not re-intercepted."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        frames = _run_with_capture(
            ["/bin/sh", "-c", "printf 'no-recursion\\n'"],
            lib,
            sock_path,
        )

    for ch, data in frames:
        assert ch in (CHANNEL_STDOUT, CHANNEL_STDIN), (
            f"unexpected channel 0x{ch:02x} with data {data!r} — possible recursion bug"
        )


def test_splice_is_captured_despite_moving_bytes_in_kernel_space() -> None:
    """splice() issues no read()/write(); the tee() peek captures it anyway."""
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        # os.splice() reaches libc's splice through the PLT. Before the hook existed
        # this produced zero frames — the bytes never touched userspace.
        frames = _run_with_capture(
            [sys.executable, "-c", "import os; os.splice(0, 1, 65536)"],
            lib,
            sock_path,
            stdin=b"spliced-payload\n",
        )

    channels = [ch for ch, _ in frames]
    assert CHANNEL_STDIN in channels, f"expected CHANNEL_STDIN from splice; got: {channels}"
    assert CHANNEL_STDOUT in channels, f"expected CHANNEL_STDOUT from splice; got: {channels}"
    stdin_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDIN)
    assert b"spliced-payload" in stdin_data
