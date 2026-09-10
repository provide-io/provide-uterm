#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures/markers for tests/tunnel/."""

from __future__ import annotations

import sys

import pytest

#: tunnel.pty_capture is POSIX-only (unguarded fcntl/pty/termios/tty) and
#: unimportable on Windows -- applied per-test rather than at class/module
#: level because several tests in the same classes don't touch pty_capture
#: and run fine on Windows.
skip_no_pty_capture = pytest.mark.skipif(
    sys.platform == "win32",
    reason="tunnel.pty_capture is POSIX-only (unguarded fcntl/pty/termios/tty) and unimportable on Windows",
)
