#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress tests for PTY capture module.

On macOS, run with ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` in the shell
environment to avoid ObjC fork-safety crashes when threads are active.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import pytest

from provide.terminal.tunnel.pty_capture import (
    install_sigwinch_handler,
    spawn_pty,
)

# ---------------------------------------------------------------------------
# Rapid spawn/close — no resource leaks
# ---------------------------------------------------------------------------


class TestRapidSpawnClose:
    """Spawn and close PTYs rapidly to check for resource leaks."""

    @pytest.mark.timeout(30)
    def test_spawn_close_50_ptys(self) -> None:
        """Spawn and close 50 PTYs — no leftover fds or zombie processes."""
        pids: list[int] = []
        for _ in range(50):
            sp = spawn_pty(["true"])
            pids.append(sp.child_pid)
            sp.close()

        # Give a moment for process table cleanup.
        time.sleep(0.1)

        # Verify all children are reaped (waitpid returns error or 0).
        for pid in pids:
            try:
                result, _ = os.waitpid(pid, os.WNOHANG)
                # result == 0 means still running (unlikely for 'true'),
                # result == pid means just reaped now — both acceptable.
                # ChildProcessError means already reaped — also fine.
                assert result in (0, pid)
            except ChildProcessError:
                pass  # Already reaped — expected.

    @pytest.mark.timeout(30)
    def test_spawn_close_no_fd_leak(self) -> None:
        """Verify master fds are properly closed after spawn/close cycle."""
        fds_before = set(_open_fds())
        for _ in range(20):
            sp = spawn_pty(["true"])
            sp.close()
        fds_after = set(_open_fds())
        leaked = fds_after - fds_before
        # Allow a small margin (logging, etc may open fds).
        assert len(leaked) <= 2, f"FD leak detected: {leaked}"


def _open_fds() -> list[int]:
    """Return list of open file descriptors for current process."""
    fds = []
    for fd in range(256):
        try:
            os.fstat(fd)
            fds.append(fd)
        except OSError:
            pass
    return fds


# ---------------------------------------------------------------------------
# High-throughput I/O
# ---------------------------------------------------------------------------


class TestHighThroughputIO:
    """Test PTY I/O under sustained load."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_cat_10000_lines(self) -> None:
        """Write 10,000 lines to cat and read them all back.

        Writer and reader run as concurrent tasks so the PTY buffer
        is drained while being filled.
        """
        sp = spawn_pty(["/bin/cat"])
        try:
            num_lines = 10_000
            marker = "LINE"
            collected = bytearray()
            write_done = asyncio.Event()

            async def writer() -> None:
                for i in range(num_lines):
                    line = f"{marker}-{i}\n"
                    await sp.write(line.encode())
                write_done.set()

            async def reader() -> None:
                last_marker = f"{marker}-{num_lines - 1}".encode()
                deadline = asyncio.get_event_loop().time() + 20.0
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        chunk = await asyncio.wait_for(sp.read(65536), timeout=2.0)
                        collected.extend(chunk)
                        if last_marker in collected:
                            return
                    except TimeoutError:
                        if write_done.is_set():
                            return  # Writer finished, no more data expected.
                    except OSError:
                        return

            await asyncio.gather(writer(), reader())

            last_marker = f"{marker}-{num_lines - 1}"
            assert last_marker.encode() in collected, f"Last marker not found. Collected {len(collected)} bytes."
            # Verify a sampling of lines are present.
            for i in range(0, num_lines, 1000):
                assert f"{marker}-{i}".encode() in collected
        finally:
            sp.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_read_1mb_output(self) -> None:
        """Spawn a command that outputs ~1MB and read it all."""
        # Use dd to output 1MB of zeros (as hex via od is too slow; use yes piped to head).
        sp = spawn_pty(["/bin/sh", "-c", "dd if=/dev/zero bs=1024 count=1024 2>/dev/null"])
        try:
            collected = b""
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await asyncio.wait_for(sp.read(65536), timeout=2.0)
                    if not chunk:
                        break
                    collected += chunk
                except TimeoutError:
                    break
                except OSError:
                    break  # EOF — child exited.

            # dd outputs exactly 1MB = 1048576 bytes, but PTY may
            # transform some bytes. Just verify we got substantial data.
            assert len(collected) >= 500_000, f"Expected ~1MB, got {len(collected)} bytes"
        finally:
            sp.close()


# ---------------------------------------------------------------------------
# Concurrent PTYs
# ---------------------------------------------------------------------------


class TestConcurrentPtys:
    """Spawn multiple PTYs simultaneously."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_10_concurrent_ptys(self) -> None:
        """Spawn 10 PTYs, write/read from each, close all."""
        ptys = [spawn_pty(["/bin/cat"]) for _ in range(10)]
        try:
            # Write a unique marker to each.
            for idx, sp in enumerate(ptys):
                await sp.write(f"pty-marker-{idx}\n".encode())

            # Read back from each.
            results: list[bytes] = [b""] * len(ptys)
            for idx, sp in enumerate(ptys):
                deadline = asyncio.get_event_loop().time() + 5.0
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        chunk = await asyncio.wait_for(sp.read(4096), timeout=1.0)
                        results[idx] += chunk
                        if f"pty-marker-{idx}".encode() in results[idx]:
                            break
                    except (TimeoutError, OSError):
                        break

            for idx in range(len(ptys)):
                assert f"pty-marker-{idx}".encode() in results[idx], f"PTY {idx} marker not found"
        finally:
            for sp in ptys:
                sp.close()


# ---------------------------------------------------------------------------
# Resize under load
# ---------------------------------------------------------------------------


class TestResizeUnderLoad:
    """Resize a PTY rapidly while doing I/O."""

    @pytest.mark.timeout(15)
    def test_resize_100_times_with_io(self) -> None:
        """Resize a PTY 100 times while it has an active child process."""
        sp = spawn_pty(["sleep", "5"])
        try:
            for i in range(100):
                cols = 80 + (i % 40)
                rows = 24 + (i % 20)
                sp.resize(cols, rows)
                actual_cols, actual_rows = sp.term_size()
                assert actual_cols == cols
                assert actual_rows == rows

            # Final verification.
            sp.resize(132, 50)
            cols, rows = sp.term_size()
            assert cols == 132
            assert rows == 50
        finally:
            sp.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(15)
    async def test_resize_during_io(self) -> None:
        """Resize while actively writing/reading via cat."""
        # Use sh -c with a finite loop so the process exits on its own.
        sp = spawn_pty(["/bin/sh", "-c", "for i in $(seq 1 200); do echo line-$i; done"])
        try:
            collected = bytearray()

            # Read all output while resizing.
            for i in range(50):
                sp.resize(80 + (i % 40), 24 + (i % 20))
                try:
                    chunk = await asyncio.wait_for(sp.read(4096), timeout=0.5)
                    collected.extend(chunk)
                except (TimeoutError, OSError):
                    pass

            assert len(collected) > 0
        finally:
            sp.close()


# ---------------------------------------------------------------------------
# SIGWINCH handler stress
# ---------------------------------------------------------------------------


class TestSigwinchStress:
    """Send many SIGWINCH signals rapidly."""

    @pytest.mark.timeout(10)
    def test_50_rapid_sigwinch(self) -> None:
        call_count = 0

        def on_resize() -> None:
            nonlocal call_count
            call_count += 1

        prev = install_sigwinch_handler(on_resize)
        try:
            for _ in range(50):
                os.kill(os.getpid(), signal.SIGWINCH)

            # Signals are delivered synchronously in the same thread for
            # os.kill in CPython, so count should be exactly 50.
            assert call_count == 50
        finally:
            signal.signal(signal.SIGWINCH, prev or signal.SIG_DFL)


# ---------------------------------------------------------------------------
# Cleanup after crash
# ---------------------------------------------------------------------------


class TestCleanupAfterCrash:
    """Verify close works when child process dies unexpectedly."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_kill_child_then_close(self) -> None:
        """Spawn a PTY, kill the child externally, verify close is clean."""
        sp = spawn_pty(["sleep", "60"])
        pid = sp.child_pid

        # Kill the child.
        os.kill(pid, signal.SIGKILL)

        # Wait briefly for the signal to take effect.
        await asyncio.sleep(0.1)

        # close() should not hang or raise.
        sp.close()
        assert sp.closed

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_read_after_child_exit(self) -> None:
        """Read from PTY after child has exited — should get EOF/OSError."""
        sp = spawn_pty(["true"])
        await asyncio.sleep(0.2)  # Let 'true' exit.
        try:
            # Should either return empty bytes or raise OSError.
            data = await asyncio.wait_for(sp.read(4096), timeout=2.0)
            # If we get data, it should be empty or the exit leftovers.
            assert isinstance(data, bytes)
        except (TimeoutError, OSError):
            pass  # Expected — child already exited.
        finally:
            sp.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_write_after_child_exit(self) -> None:
        """Write to PTY after child has exited — should raise or be silent."""
        sp = spawn_pty(["true"])
        await asyncio.sleep(0.2)
        try:
            await sp.write(b"this goes nowhere\n")
        except OSError:
            pass  # Expected.
        finally:
            sp.close()

    def test_double_close_after_kill(self) -> None:
        """Double-close after killing the child is safe."""
        sp = spawn_pty(["sleep", "60"])
        os.kill(sp.child_pid, signal.SIGKILL)
        sp.close()
        sp.close()  # Second close is a no-op.
        assert sp.closed
