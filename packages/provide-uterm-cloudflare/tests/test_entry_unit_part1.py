#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for entry.py — Default.fetch() dispatch logic."""

from __future__ import annotations

from provide.uterm.cloudflare.entry.registry import _extract_worker_id

# ---------------------------------------------------------------------------
# _extract_worker_id
# ---------------------------------------------------------------------------


def test_extract_worker_id_ws_browser() -> None:
    assert _extract_worker_id("/ws/browser/my-session/term") == "my-session"


def test_extract_worker_id_ws_worker() -> None:
    assert _extract_worker_id("/ws/worker/sess-123/term") == "sess-123"


def test_extract_worker_id_ws_raw() -> None:
    assert _extract_worker_id("/ws/raw/raw-sess/term") == "raw-sess"


def test_extract_worker_id_worker_hijack() -> None:
    assert _extract_worker_id("/worker/abc/hijack/acquire") == "abc"


def test_extract_worker_id_worker_input_mode() -> None:
    assert _extract_worker_id("/worker/abc/input_mode") == "abc"


def test_extract_worker_id_unknown() -> None:
    assert _extract_worker_id("/api/health") is None
