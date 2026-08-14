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
through the dynamic symbol table. Hooked: read(), write(), and the three
kernel-space copies that issue no read/write of their own — splice() (captured by
peeking with tee(), which duplicates pipe data without consuming it) plus
sendfile() and copy_file_range() (captured by re-reading the moved range from
their regular-file source, which the copy leaves intact).

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
from provide.uterm.pty.capture import CHANNEL_STATS, CHANNEL_STDIN, CHANNEL_STDOUT


def _require_linux_and_lib() -> Path:
    """Skip unless on Linux with libuterm_capture.so present."""
    if sys.platform != "linux":
        pytest.skip("LD_PRELOAD capture only supported on Linux (macOS SIP blocks it)")
    lib = get_capture_lib_path()
    if lib is None:
        pytest.skip("libuterm_capture.so not built — run 'make' in native/capture/")
    return lib


def _serve_once(sock_path: str, timeout: float = 3.0, ready: threading.Event | None = None) -> bytes:
    """
    Listen on a Unix socket, accept one connection, read all data, return raw bytes.

    Runs synchronously in a thread so it doesn't interfere with the asyncio loop.

    ``ready`` is set once the socket is listening, and the caller must wait on it
    before starting the process being captured. Without that handshake the
    captured process can reach its ``connect()`` before this thread reaches
    ``listen()``: the shim finds nothing to connect to, writes no frames, and
    exits -- after which ``accept()`` here waits out the whole timeout and the
    test fails with "socket collector raised: timed out".

    That race is why these tests failed intermittently, a DIFFERENT one each run
    (test_all_three_words_arrive_in_stdout,
    test_library_does_not_intercept_non_stdio_fds,
    test_copy_file_range_is_captured_when_stdout_is_a_file), and more often on a
    loaded machine where this thread is slower to be scheduled. Nothing was
    wrong with the shim; the test never gave it a socket to find.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(sock_path)
        s.listen(1)
        s.settimeout(timeout)
        if ready is not None:
            ready.set()
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
    ready = threading.Event()

    def collect() -> None:
        try:
            raw_holder.append(_serve_once(sock_path, timeout=timeout, ready=ready))
        except Exception as exc:
            exc_holder.append(exc)
        finally:
            # Also on the failure path, or a bind that raised would leave the
            # main thread waiting below for the full timeout before reporting an
            # error it already has.
            ready.set()

    t = threading.Thread(target=collect)
    t.start()
    if not ready.wait(timeout):
        pytest.fail("socket collector never started listening")

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


def test_a_lossless_session_still_reports_its_counters() -> None:
    """A healthy tap must say so, not just a lossy one.

    Reporting only when a counter moves makes silence mean four different
    things -- delivering everything, never loaded, writer never ready, disabled
    -- which are exactly the states these counters exist to separate. Measured
    against a live deck painting for 65s: the consumer sat on 'no report yet'
    for the whole session while the tap was in fact delivering losslessly, so a
    working capture path and a dead one read identically.

    The baseline report is what makes ``ready=1 enabled=1`` observable.
    """
    lib = _require_linux_and_lib()

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        # Comfortably past STATS_MIN_ATTEMPTS (64) so the rate limit is not
        # what decides the outcome, and small enough not to provoke a drop.
        frames = _run_with_capture(
            ["/bin/sh", "-c", "i=0; while [ $i -lt 200 ]; do printf 'line-%s\\n' $i; i=$((i+1)); done"],
            lib,
            sock_path,
            timeout=20,
        )

    stats = [data for ch, data in frames if ch == CHANNEL_STATS]
    assert stats, f"no CHANNEL_STATS frame from a lossless run; channels: {sorted({ch for ch, _ in frames})}"
    report = stats[-1].decode()
    assert "ready=1" in report, report
    assert "enabled=1" in report, report
    # The run lost nothing, so the baseline is what got this frame sent.
    assert "busy=0 wouldblock=0 invalid=0 disabled=0" in report, report


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
        ready = threading.Event()

        def collect() -> None:
            try:
                raw_holder.append(_serve_once(sock_path, timeout=0.5, ready=ready))
            except OSError:
                raw_holder.append(b"")
            finally:
                ready.set()

        t = threading.Thread(target=collect)
        t.start()
        # Wait for the listen() before starting the process. This assertion is
        # "nothing connected", and without the handshake it would also hold when
        # nothing was ever LISTENING -- passing for the wrong reason, which is
        # the failure mode a negative test has to be built against.
        if not ready.wait(5):
            pytest.fail("socket collector never started listening")

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


def test_sendfile_is_captured_by_re_reading_the_source() -> None:
    """sendfile() copies a file straight to fd 1; the moved range is recovered."""
    lib = _require_linux_and_lib()

    child = (
        "import os, tempfile\n"
        "fd, path = tempfile.mkstemp()\n"
        'os.write(fd, b"sendfile-payload\\n"); os.close(fd)\n'
        "src = os.open(path, os.O_RDONLY)\n"
        "os.sendfile(1, src, 0, 64)\n"
    )
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        frames = _run_with_capture([sys.executable, "-c", child], lib, sock_path)

    channels = [ch for ch, _ in frames]
    assert CHANNEL_STDOUT in channels, f"expected CHANNEL_STDOUT from sendfile; got: {channels}"
    stdout_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDOUT)
    assert b"sendfile-payload" in stdout_data


def test_copy_file_range_is_captured_when_stdout_is_a_file() -> None:
    """copy_file_range() is file-to-file, so it only reaches fd 1 when redirected."""
    lib = _require_linux_and_lib()

    child = (
        "import os, tempfile\n"
        "fd, src_p = tempfile.mkstemp()\n"
        'os.write(fd, b"cfr-payload\\n"); os.close(fd)\n'
        "src = os.open(src_p, os.O_RDONLY)\n"
        'dst = os.open(src_p + ".out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n'
        "os.dup2(dst, 1)\n"  # fd 1 must be a regular file for copy_file_range
        "os.copy_file_range(src, 1, 64)\n"
    )
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "cap.sock")
        frames = _run_with_capture([sys.executable, "-c", child], lib, sock_path)

    channels = [ch for ch, _ in frames]
    assert CHANNEL_STDOUT in channels, f"expected CHANNEL_STDOUT from copy_file_range; got: {channels}"
    stdout_data = b"".join(data for ch, data in frames if ch == CHANNEL_STDOUT)
    assert b"cfr-payload" in stdout_data
