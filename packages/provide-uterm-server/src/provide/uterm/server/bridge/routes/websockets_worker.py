#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Worker-side WebSocket frame helpers for the hijack hub.

Extracted from ``websockets_impl.py`` to keep that module under the source-size
cap. These module-level helpers drive the ``ws_worker_term`` recv loop:

- ``_handle_worker_hello`` — protocol negotiation + input-mode application
  (runs OUTSIDE the per-frame builder guard; carries no validated builder).
- ``_build_worker_frame`` — validated wire-frame construction (runs INSIDE the
  narrow per-frame builder guard).
- ``_dispatch_worker_frame`` — downstream I/O for a built frame (runs OUTSIDE
  the guard so a genuine server-side fault propagates).
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.bridge.contracts import (
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    negotiate_protocol_version,
)
from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import (
    coerce_worker_status_frame,
    make_analysis_frame,
    make_snapshot_frame,
)
from provide.uterm.server.bridge.models import _safe_float, _safe_int
from provide.uterm.server.bridge.rest_helpers import extract_prompt_id

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub import TermHub
else:
    WebSocket = Any

logger = get_logger(__name__)


async def _handle_worker_hello(hub: TermHub, websocket: WebSocket, worker_id: str, msg: dict[str, Any]) -> bool:
    """Handle a ``worker_hello`` frame: negotiate protocol + apply input mode.

    Returns ``True`` if the caller should ``break`` the recv loop (protocol
    mismatch → 1002 close), ``False`` to ``continue``. Carries no validated
    frame builder, so it lives OUTSIDE the per-frame builder guard.
    """
    _hello_mode = msg.get("input_mode")
    # Protocol range negotiation. Workers may send either the legacy
    # ``protocol_version`` field (single int) or the new ``protocol`` object
    # ``{min, max, preferred}``. Absent fields default to ``{min=1, max=1}`` —
    # preserves behaviour for old clients that never advertised.
    _proto_block = msg.get("protocol")
    if isinstance(_proto_block, dict):
        _client_min = _safe_int(_proto_block.get("min"), MIN_PROTOCOL_VERSION, min_val=1)
        _client_max = _safe_int(_proto_block.get("max"), MAX_PROTOCOL_VERSION, min_val=1)
    elif "protocol_version" in msg:
        _legacy_v = _safe_int(msg.get("protocol_version"), 0)
        _client_min = _legacy_v if _legacy_v >= 1 else 1
        _client_max = _client_min
    else:
        _client_min = 1
        _client_max = 1
    _selected = negotiate_protocol_version(_client_min, _client_max)
    if _selected is None:
        # No overlap → close 1002. Send a structured error frame first so the
        # worker can surface a meaningful disconnect reason.
        logger.warning(
            "worker_hello_protocol_mismatch worker_id=%s client=[%d,%d] server=[%d,%d]",
            worker_id,
            _client_min,
            _client_max,
            MIN_PROTOCOL_VERSION,
            MAX_PROTOCOL_VERSION,
        )
        with suppress(Exception):
            await websocket.send_text(
                encode_control_frame(
                    {
                        "type": "error",
                        "reason": "protocol_mismatch",
                        "client_min": _client_min,
                        "client_max": _client_max,
                        "server_min": MIN_PROTOCOL_VERSION,
                        "server_max": MAX_PROTOCOL_VERSION,
                    }
                )
            )
            await websocket.close(code=1002, reason="protocol_mismatch")
        return True
    if _hello_mode in ("hijack", "open"):
        mode_applied = await hub.set_worker_hello(worker_id, _hello_mode, _selected)
        if mode_applied:  # pragma: no branch — set_worker_hello returns False only on a missing worker registration, already filtered upstream
            await hub.broadcast_hijack_state(worker_id)
        logger.info(
            "worker_hello worker_id=%s input_mode=%s protocol_selected=%d applied=%s",
            worker_id,
            _hello_mode,
            _selected,
            mode_applied,
        )
    elif _hello_mode is not None:
        logger.warning(
            "worker_hello_invalid_mode worker_id=%s input_mode=%r — expected 'hijack' or 'open', ignoring",
            worker_id,
            _hello_mode,
        )
    return False


def _build_worker_frame(mtype: str, msg: dict[str, Any]) -> dict[str, Any]:
    """Build the validated wire frame for a worker control frame.

    Runs INSIDE the narrow per-frame guard: a malformed worker frame makes the
    builder raise (ValidationError/ValueError/KeyError/TypeError), which the
    caller isolates. No state is mutated here, so a build failure cannot leave
    partial state. The matching downstream I/O lives in
    ``_dispatch_worker_frame`` and runs OUTSIDE the guard.
    """
    if mtype == "snapshot":
        return cast(
            "dict[str, Any]",
            make_snapshot_frame(
                screen=str(msg.get("screen", "")),
                cursor=cast("dict[str, int]", msg.get("cursor", {"x": 0, "y": 0})),
                cols=_safe_int(msg.get("cols"), 80, min_val=1),
                rows=_safe_int(msg.get("rows"), 25, min_val=1),
                screen_hash=str(msg.get("screen_hash", "")),
                cursor_at_end=bool(msg.get("cursor_at_end", True)),
                has_trailing_space=bool(msg.get("has_trailing_space", False)),
                prompt_detected=cast("dict[str, Any] | None", msg.get("prompt_detected")),
                raw_tail=cast("str | None", msg.get("raw_tail")),
                ts=_safe_float(msg.get("ts"), time.time()),
            ),
        )
    if mtype == "analysis":
        return cast(
            "dict[str, Any]",
            make_analysis_frame(
                formatted=str(msg.get("formatted", "")),
                raw=msg.get("raw"),
                ts=_safe_float(msg.get("ts"), time.time()),
            ),
        )
    # mtype == "status" — the only remaining builder branch (filtered upstream).
    return cast("dict[str, Any]", coerce_worker_status_frame(msg))


async def _dispatch_worker_frame(hub: TermHub, worker_id: str, mtype: str, frame: dict[str, Any]) -> None:
    """Run the downstream I/O for a successfully-built worker frame.

    Runs OUTSIDE the per-frame builder guard: a failure here (update / broadcast
    / append_event / redaction) is a genuine server-side fault and must
    propagate to the outer handler, NOT be mis-isolated as an "invalid worker
    frame" (which would mis-count it and silently swallow a real bug).
    """
    if mtype == "snapshot":
        await hub.update_last_snapshot(worker_id, frame)
        await hub.broadcast(worker_id, frame)
        await hub.append_event(
            worker_id,
            "snapshot",
            {
                "prompt_id": extract_prompt_id(frame),
                "screen_hash": frame.get("screen_hash"),
                "screen": frame.get("screen", ""),
            },
        )
    elif mtype == "analysis":
        await hub.broadcast(worker_id, frame)
    else:  # mtype == "status"
        await hub.broadcast(worker_id, frame)
        await hub.append_event(worker_id, "worker_status", {"status": frame})
