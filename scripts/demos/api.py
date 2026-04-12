#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTTP API helpers for demo recording scripts."""

from __future__ import annotations

import time

from scripts.demos.output import clean_terminal_output


def wait_connected(base_url: str, session_id: str, timeout: float = 15.0) -> bool:
    """Poll until the session reports connected=True."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/sessions/{session_id}", timeout=5.0)
            if r.status_code == 200 and r.json().get("connected"):
                return True
        except Exception:  # noqa: S110
            pass
        time.sleep(0.3)
    return False


def send_to_session(
    base_url: str,
    session_id: str,
    text: str,
    wait_s: float = 1.5,
) -> bool:
    """Acquire a hijack lease, send terminal input, release.

    Switches the session to hijack mode, sends the given keystrokes, waits for
    output to settle, then releases and restores open mode. Returns True on success.
    """
    import httpx as _httpx

    try:
        with _httpx.Client(base_url=base_url, timeout=30.0) as http:
            http.patch(f"/api/sessions/{session_id}", json={"input_mode": "hijack"})
            r = http.post(
                f"/worker/{session_id}/hijack/acquire",
                json={"owner": "demo-setup", "lease_s": 30},
            )
            if r.status_code != 200:
                return False
            hijack_id = r.json()["hijack_id"]
            http.post(
                f"/worker/{session_id}/hijack/{hijack_id}/send",
                json={"keys": text},
            )
            time.sleep(wait_s)
            http.post(f"/worker/{session_id}/hijack/{hijack_id}/release")
            http.patch(f"/api/sessions/{session_id}", json={"input_mode": "open"})
            return True
    except Exception:
        return False


def fanout_send(
    base_url: str,
    group_id: str,
    text: str,
    wait_s: float = 2.5,
) -> bool:
    """Broadcast a command to all workers in a fanout group and wait for responses."""
    import httpx as _httpx

    try:
        with _httpx.Client(base_url=base_url, timeout=30.0) as http:
            r = http.post(
                f"/api/fanout/groups/{group_id}/send",
                json={"data": text, "quiesce_ms": 1500, "max_response_ms": 5000},
            )
            if r.status_code != 200:
                return False
            time.sleep(wait_s)
            return True
    except Exception:
        return False


def fanout_send_results(
    base_url: str,
    group_id: str,
    text: str,
    wait_s: float = 2.5,
) -> list[dict[str, str]]:
    """Broadcast a command and return per-worker cleaned output strings.

    Returns list of {worker_id, output} dicts. output has ANSI stripped and
    only the last meaningful non-prompt line is kept.
    """
    import httpx as _httpx

    try:
        with _httpx.Client(base_url=base_url, timeout=30.0) as http:
            r = http.post(
                f"/api/fanout/groups/{group_id}/send",
                json={"data": text, "quiesce_ms": 1500, "max_response_ms": 5000},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            time.sleep(wait_s)
            return [
                {"worker_id": res.get("worker_id", "?"), "output": clean_terminal_output(res.get("output_delta", ""))}
                for res in data.get("results", [])
            ]
    except Exception:
        return []
