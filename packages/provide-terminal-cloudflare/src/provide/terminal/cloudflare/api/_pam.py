# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
POST /api/pam-events — accept PAM session notifications from a local bridge.

The pam_uterm.so module writes to a Unix socket on the SSH host.  A local
bridge (e.g. scripts/deckmux_demo_server.py or a dedicated forwarder) reads
those events and POSTs them here so the operator dashboard can reflect
live SSH sessions.

Wire format (same JSON as pam_uterm.so):
  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"notify"}
  {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345}

On "open" (notify mode), a read-only observer session entry is written to KV.
On "close", the session is removed from KV.

Capture mode (LD_PRELOAD) is a local-server-only capability — the CF edge
cannot receive a Unix socket connection from the SSH host, so capture events
are accepted but treated identically to notify events (session visible in
dashboard but no live I/O).
"""

from __future__ import annotations

import re
import time
from typing import Any

_TTY_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _tty_slug(tty: str) -> str:
    """/dev/pts/3 → last component slug."""
    basename = tty.split("/")[-1] if "/" in tty else tty
    return _TTY_SLUG_RE.sub("-", basename).strip("-") or "tty"


async def handle_pam_event(request: object, env: object) -> object:
    """Handle POST /api/pam-events."""
    try:
        from provide.terminal.cloudflare.cf_types import json_response
    except ImportError:
        from cf_types import json_response  # type: ignore[import-not-found]

    method = str(getattr(request, "method", "GET")).upper()
    if method != "POST":
        return json_response({"error": "method_not_allowed"}, status=405)

    try:
        raw = await request.json()  # type: ignore[union-attr]
        body: dict[str, Any] = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        return json_response({"error": "invalid_json"}, status=400)

    event = str(body.get("event") or "")
    if event not in ("open", "close"):
        return json_response({"error": "unknown_event", "event": event}, status=422)

    username = str(body.get("username") or "")
    if not username:
        return json_response({"error": "missing_username"}, status=422)

    tty = str(body.get("tty") or "")
    slug = _tty_slug(tty)
    session_id = f"pam-{username}-{slug}"

    kv = getattr(env, "SESSION_REGISTRY", None)

    if event == "open":
        entry: dict[str, Any] = {
            "session_id": session_id,
            "display_name": f"{username} ({tty or 'pam'})",
            "created_at": time.time(),
            "connector_type": "shell",
            "lifecycle_state": "running",
            "input_mode": "open",
            "connected": True,
            "auto_start": False,
            "tags": ["pam", str(body.get("mode") or "notify"), username],
            "recording_enabled": False,
            "recording_available": False,
            "owner": username,
            "visibility": "operator",
            "last_error": None,
        }
        if kv is not None:
            import json as _json

            await kv.put(f"session:{session_id}", _json.dumps({**entry, "hijacked": False}))
        return json_response({"ok": True, "session_id": session_id, "action": "created"})

    # event == "close"
    if kv is not None:
        await kv.delete(f"session:{session_id}")
    return json_response({"ok": True, "session_id": session_id, "action": "deleted"})
