#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A capture socket takes over only a socket nobody is listening on.

asyncio removes whatever socket is at the path before it binds. That is right
for one a crashed server left behind, and wrong for one another session is
still serving, which would lose its producer to the new one. So a socket is
probed first: only one whose connect is refused is stale.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from provide.uterm.pty import capture
from provide.uterm.pty.capture import CaptureSocket


def _stale(path: str) -> None:
    """A socket file with no listener behind it, as a killed server leaves one."""
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(path)
    left.close()


async def test_a_stale_socket_is_taken_over() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        _stale(path)
        capture = CaptureSocket(path)
        await capture.start()
        reader, writer = await asyncio.open_unix_connection(path)
        writer.close()
        await writer.wait_closed()
        await capture.stop()


async def test_a_socket_another_session_serves_is_left_to_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        first = CaptureSocket(path)
        await first.start()
        second = CaptureSocket(path)
        with pytest.raises(OSError, match=r"capture socket is in use") as caught:
            await second.start()
        assert caught.value.errno == errno.EADDRINUSE
        # The first still has its path, and still answers on it.
        reader, writer = await asyncio.open_unix_connection(path)
        writer.close()
        await writer.wait_closed()
        await first.stop()


async def test_a_socket_that_will_not_say_is_left_alone() -> None:
    # Not refused, so not provably stale: this user may not even connect to it.
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        _stale(path)
        os.chmod(path, 0)  # noqa: PTH101
        with pytest.raises(OSError, match=r"capture socket is in use"):
            await CaptureSocket(path).start()
        assert Path(path).is_socket()


async def test_a_file_that_is_not_a_socket_is_left_for_the_bind_to_refuse() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cap.sock"
        path.write_text("not a socket")
        with pytest.raises(OSError) as caught:
            await CaptureSocket(str(path)).start()
        assert "capture socket is in use" not in str(caught.value)
        assert path.read_text() == "not a socket"


async def test_the_probe_does_not_hold_the_event_loop() -> None:
    # A listener too busy to answer holds the probe up to its timeout; the
    # loop serves every other terminal meanwhile.
    loop_thread = threading.get_ident()
    probed_on: list[int] = []
    real = capture._refuse_live_socket

    def probe(path: str) -> None:
        probed_on.append(threading.get_ident())
        real(path)

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        _stale(path)
        sock = CaptureSocket(path)
        with patch.object(capture, "_refuse_live_socket", probe):
            await sock.start()
        await sock.stop()
    assert probed_on
    assert loop_thread not in probed_on
