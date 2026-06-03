#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
Regression: capture framing must stay intact under concurrent multithreaded writes.

``send_frame`` in ``native/capture/capture.c`` used to emit the 5-byte header and
the payload as TWO separate syscalls on the shared, unlocked ``g_capture_fd``. A
multithreaded captured process could then interleave one thread's header with
another thread's payload, corrupting the length-prefixed framing the Python
reader (:class:`CaptureSocket`) depends on. The fix concatenates header+payload
into one buffer and emits a single write()/send(), making each frame atomic.

This test drives the *real compiled* capture library (LD_PRELOAD on Linux,
DYLD_INSERT_LIBRARIES on macOS) with a tiny C program whose threads each call
``write(1, ...)`` with NO lock, then asserts every received byte parses into a
well-formed, complete frame. Before the fix this fails deterministically (the
reader sees bunched headers / torn payloads); after it, all frames are intact.

It is skipped when no C compiler or no built capture library is available.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import tempfile
import threading
from pathlib import Path
from shutil import which

import pytest

from provide.uterm.pty._build import get_capture_lib_path
from provide.uterm.pty.capture import CHANNEL_STDIN, CHANNEL_STDOUT

# Each emitted line is exactly LINE_LEN bytes including the trailing newline and
# is self-describing ("Txx-yyyy-PPP...\n") so the parser can spot any tearing.
_LINE_LEN = 32
_NTHREADS = 16
_NWRITES = 400

# A multithreaded writer: NTHREADS pthreads each issue NWRITES unlocked write(1)
# calls. This is the minimal reproducer — Python's GIL serialises write() and so
# would hide the race; only genuinely concurrent libc write() calls expose it.
# The defines are derived from the Python constants so both sides stay in sync;
# the rest is a literal (no str-format) to keep C's own %-specifiers intact.
_MT_WRITER_DEFINES = f"#define NTHREADS {_NTHREADS}\n#define NWRITES  {_NWRITES}\n#define LINE_LEN {_LINE_LEN}\n"
_MT_WRITER_C = (
    "#include <pthread.h>\n"
    "#include <stdio.h>\n"
    "#include <string.h>\n"
    "#include <unistd.h>\n\n"
    + _MT_WRITER_DEFINES
    + r"""
static void *worker(void *arg) {
    long tid = (long)arg;
    char line[64];
    for (int j = 0; j < NWRITES; j++) {
        memset(line, 'P', sizeof(line));
        int k = snprintf(line, sizeof(line), "T%02ld-%04d-", tid, j);
        line[k] = 'P';            /* overwrite snprintf's NUL */
        line[LINE_LEN - 1] = '\n';
        (void)write(1, line, LINE_LEN);
    }
    return NULL;
}

int main(void) {
    pthread_t th[NTHREADS];
    for (long i = 0; i < NTHREADS; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < NTHREADS; i++) pthread_join(th[i], NULL);
    return 0;
}
"""
)


def _capture_env(lib: Path, sock_path: str) -> dict[str, str]:
    """Env that injects the capture lib for the host platform."""
    env = {**os.environ, "UTERM_CAPTURE_SOCKET": sock_path}
    if os.uname().sysname == "Darwin":
        env["DYLD_INSERT_LIBRARIES"] = str(lib)
        # Flat namespace lets the interposers bind into the (non-SIP) test binary.
        env["DYLD_FORCE_FLAT_NAMESPACE"] = "1"
    else:
        env["LD_PRELOAD"] = str(lib)
    return env


def _require_lib_and_cc() -> Path:
    """Skip unless a built capture lib and a C compiler are both available."""
    lib = get_capture_lib_path()
    if lib is None:
        pytest.skip("capture library not built — run 'make' in native/capture/")
    if which("cc") is None:
        pytest.skip("no C compiler (cc) available to build the multithreaded writer")
    return lib


def _build_mt_writer(workdir: Path) -> Path:
    """Compile the multithreaded stdout writer; skip if the toolchain fails."""
    src = workdir / "mt_writer.c"
    out = workdir / "mt_writer"
    src.write_text(_MT_WRITER_C)
    proc = subprocess.run(
        ["cc", "-O2", "-o", str(out), str(src), "-lpthread"],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not compile multithreaded writer: {proc.stderr.decode(errors='replace')}")
    return out


def _serve_once(sock_path: str, out: list[bytes], timeout: float = 10.0) -> None:
    """Accept one connection on a Unix socket and collect all bytes into *out*."""
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
                d = conn.recv(65536)
                if not d:
                    break
                chunks.append(d)
        except OSError:
            pass
        finally:
            conn.close()
        out.append(b"".join(chunks))
    finally:
        s.close()


def _parse_strict(raw: bytes) -> tuple[list[tuple[int, bytes]], bool]:
    """
    Parse length-prefixed frames, flagging any framing corruption.

    Returns (frames, corrupt). ``corrupt`` is True if a header references an
    impossible channel/length or the stream does not divide cleanly into frames
    — exactly the symptoms produced by interleaved header/payload syscalls.
    """
    frames: list[tuple[int, bytes]] = []
    i = 0
    corrupt = False
    while i + 5 <= len(raw):
        channel = raw[i]
        (length,) = struct.unpack(">I", raw[i + 1 : i + 5])
        if channel not in (CHANNEL_STDOUT, CHANNEL_STDIN) or i + 5 + length > len(raw) or length > (1 << 20):
            corrupt = True
            break
        frames.append((channel, raw[i + 5 : i + 5 + length]))
        i += 5 + length
    if i != len(raw):
        corrupt = True
    return frames, corrupt


def test_concurrent_writes_keep_framing_atomic() -> None:
    """Concurrent multithreaded write()s must not tear the capture framing."""
    lib = _require_lib_and_cc()

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        writer = _build_mt_writer(workdir)
        sock_path = str(workdir / "cap.sock")

        out: list[bytes] = []
        t = threading.Thread(target=_serve_once, args=(sock_path, out))
        t.start()

        proc = subprocess.run(
            [str(writer)],
            env=_capture_env(lib, sock_path),
            capture_output=True,
            timeout=30,
        )
        _ = proc  # returncode not asserted — capture is best-effort
        t.join(timeout=15)

    raw = out[0] if out else b""
    if not raw:
        pytest.skip("capture library did not intercept (e.g. SIP / static binary) — nothing to assert")

    frames, corrupt = _parse_strict(raw)
    assert not corrupt, f"capture framing corrupted under concurrency: {len(frames)} frames parsed from {len(raw)}B"

    # Every reassembled stdout line must be a well-formed, untorn record.
    payload = b"".join(data for ch, data in frames if ch == CHANNEL_STDOUT)
    records = [r for r in payload.split(b"\n") if r]
    malformed = [r for r in records if not (r.startswith(b"T") and len(r) == _LINE_LEN - 1 and r[3:4] == b"-")]
    assert not malformed, f"{len(malformed)} torn records, e.g. {malformed[:3]!r}"
