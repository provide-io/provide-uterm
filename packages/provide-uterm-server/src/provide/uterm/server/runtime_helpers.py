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
import re
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


# An escape sequence cut off at the end of a streamed chunk: a bare ESC, or a CSI
# (ESC [) whose parameter/intermediate bytes have not yet reached a final byte.
# The grammar is exactly that of ``provide.uterm.strip_ansi`` (screen.py's
# ``_ANSI_ESCAPE_RE``): anything that pattern would strip once complete, and
# nothing else, is held back.
_INCOMPLETE_ESCAPE_TAIL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*)?")
# Longest tail held back between chunks, in characters, ESC included. A real
# sequence is a handful of bytes; past this the "sequence" is not one (or is
# hostile), and it is released into the text rather than carried forever.
_MAX_ESCAPE_CARRY = 64


def _split_incomplete_escape(text: str) -> tuple[str, str]:
    """Split *text* into (complete, carry): carry is an unterminated trailing escape.

    ``strip_ansi`` sees one chunk at a time, so ``"\x1b[1"`` + ``"msudo"`` would
    strip to ``"\x1b[1"`` (no final byte, left alone) and ``"msudo"``, and
    ``\bsudo\b`` would miss. The caller prepends the carry to the next chunk.
    Only the LAST ESC can start an unterminated tail (CSI parameter bytes never
    include ESC). A tail longer than ``_MAX_ESCAPE_CARRY`` is not carried.
    """

    start = text.rfind("\x1b")
    if start < 0 or len(text) - start > _MAX_ESCAPE_CARRY:
        return text, ""
    if _INCOMPLETE_ESCAPE_TAIL.fullmatch(text, start) is None:
        return text, ""
    return text[:start], text[start:]


# Bound on the per-recording set of read-path annotation keys. Past it the set is
# cleared and starts over: simple, and the worst case is that a snapshot repeats
# an annotation the stream already recorded once more than 1024 distinct
# read-path matches ago.
_MAX_READ_ANNOTATION_KEYS = 1024


def _read_annotation_key(annotation: Annotation) -> str:
    """Identity of a read-path annotation for snapshot dedupe: rule + matched text.

    ``Annotation`` carries no rule id; ``label`` is the rule's category label and
    ``description`` is the rule's own template formatted with the match (cut at
    80 characters). So the pair names the rule and, for every rule whose
    template embeds ``{match}``, the matched text. Credential rules deliberately
    never embed the match, so for them the key is the rule alone.
    """

    return f"{annotation.label}\x00{annotation.description}"


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
