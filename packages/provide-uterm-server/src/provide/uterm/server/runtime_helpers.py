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

    from provide.uterm.annotation import Annotation


def _encode_runtime_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_terminal_data(str(msg.get("data") or ""))
    return encode_control_frame(msg)


# Bound on each per-recording set of read-path annotation keys (see
# HostedSessionRuntime._read_annotation_keys / _snapshot_only_keys). Past it the
# set is cleared and starts over: simple, and the worst case is one repeated (or,
# for the snapshot-only set, one extra) annotation more than 1024 distinct
# read-path matches after the first.
_MAX_READ_ANNOTATION_KEYS = 1024


def _add_bounded(keys: set[str], key: str) -> None:
    """Add *key* to *keys*, first clearing the set when it holds _MAX_READ_ANNOTATION_KEYS."""
    if len(keys) >= _MAX_READ_ANNOTATION_KEYS:
        keys.clear()
    keys.add(key)


def _read_annotation_key(annotation: Annotation) -> str:
    """Identity of a read-path annotation for stream/snapshot dedupe.

    ``annotation.match_key`` as set by the detector:
    ``sha256(rule_id + "\\x00" + full matched text)``, lowercase hex
    (``provide.uterm.annotation.annotation_match_key``). It names the rule AND
    the exact text matched -- so two different AWS keys, which share a label and
    a description (credential rules never embed the match), are two keys -- and
    it is a digest, so the dedupe sets never hold a raw secret. It is not part of
    the recorded annotation (``Annotation.to_dict`` omits it).

    It is "" for an annotation the detector did not build. An EMPTY key is never
    deduped: the runtime records that annotation and adds it to neither set
    (otherwise every such annotation would collapse into one key).
    """

    return annotation.match_key


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
