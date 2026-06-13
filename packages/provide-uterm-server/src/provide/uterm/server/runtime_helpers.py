#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stateless helpers for the hosted session runtime.

These module-level functions carry no per-session instance state; they are
factored out of ``runtime.py`` so that file stays under 500 LOC. The public
import surface is unchanged — ``runtime`` re-exports every name below.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Literal

from provide.uterm.control_channel import encode_control_frame, encode_terminal_data
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

if TYPE_CHECKING:
    from collections.abc import Callable


def _encode_runtime_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_terminal_data(str(msg.get("data") or ""))
    return encode_control_frame(msg)


def _build_recording_redactor(enabled: bool) -> Callable[[str], str] | None:
    if not enabled:
        return None
    redactor = StreamRedactor(default_rules())
    return redactor.redact


# Outcome of one ``_run_one_attempt`` failure — drives whether the outer
# loop retries with backoff, gives up immediately, or treats the attempt
# as successful (cancelled).
RunOutcome = Literal["cancelled", "permanent", "retry"]


def _classify_run_error(exc: BaseException) -> RunOutcome:
    """Classify an exception from ``_run_one_attempt`` for retry policy.

    - ``cancelled`` — caller initiated shutdown; break the loop cleanly.
    - ``permanent`` — ``ValueError`` (config error) or HTTP 4xx auth/path
      failure. No backoff will recover; break.
    - ``retry`` — everything else; sleep on backoff and try again.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, ValueError):
        return "permanent"
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403, 404):
        return "permanent"
    return "retry"


async def _cancel_and_wait(tasks: set[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_task_completion(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await task
