#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, cast

__all__ = [
    "MAX_EXPECT_REGEX_LEN",
    "PromptRegexError",
    "build_hijack_events_response",
    "build_hijack_snapshot_response",
    "compile_expect_regex",
    "extract_prompt_id",
    "snapshot_matches",
]

if TYPE_CHECKING:
    from provide.uterm.server.bridge.rest_helpers import (
        MAX_EXPECT_REGEX_LEN,
        PromptRegexError,
        build_hijack_events_response,
        build_hijack_snapshot_response,
        extract_prompt_id,
        snapshot_matches,
    )
else:
    try:
        from provide.uterm.server.bridge.rest_helpers import (
            MAX_EXPECT_REGEX_LEN,
            PromptRegexError,
            build_hijack_events_response,
            build_hijack_snapshot_response,
            extract_prompt_id,
            snapshot_matches,
        )
    except (ImportError, ModuleNotFoundError):  # pragma: no cover
        # Fallback for Cloudflare Durable Objects validation phase where
        # provide.uterm modules are not in the import path yet.
        # These will be imported at runtime when actually needed.
        MAX_EXPECT_REGEX_LEN = 10000
        PromptRegexError = Exception

        def build_hijack_events_response(*_args, **_kwargs):
            raise RuntimeError("Should not be called during validation")

        def build_hijack_snapshot_response(*_args, **_kwargs):
            raise RuntimeError("Should not be called during validation")

        def extract_prompt_id(*_args, **_kwargs):
            raise RuntimeError("Should not be called during validation")

        def snapshot_matches(*_args, **_kwargs):
            raise RuntimeError("Should not be called during validation")


if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol


def _looks_like_counted_quantifier(pattern: str, start: int) -> bool:
    end = pattern.find("}", start + 1)
    if end == -1:
        return False
    body = pattern[start + 1 : end]
    if not body:
        return False
    left, separator, right = body.partition(",")
    return left.isdigit() and (not separator or right == "" or right.isdigit())


def _validate_pattern_safety(pattern: str) -> None:
    """Enforce a conservative grammar with at most one branching quantifier.

    Python's backtracking engine has no match timeout. Prompt guards therefore
    reject constructs whose cost cannot be bounded linearly: alternation,
    backreferences, regex extensions/lookarounds, and multiple variable-width
    quantifiers (including sequential ``a*a*`` or ``a?a?`` chains).
    """
    branching_quantifiers = 0
    escaped = False
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if escaped:
            if char.isdigit() or char == "g":
                raise ValueError("backreferences are not allowed")
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            continue
        if char == "|":
            raise ValueError("alternation is not allowed")
        if char == "(" and index + 1 < len(pattern) and pattern[index + 1] == "?":
            raise ValueError("regex extensions and lookarounds are not allowed")
        variable_quantifier = char in "+*?"
        if char == "{" and _looks_like_counted_quantifier(pattern, index):
            end = pattern.find("}", index)
            variable_quantifier = "," in pattern[index + 1 : end]
            index = end
        if variable_quantifier:
            branching_quantifiers += 1
            if branching_quantifiers > 1:
                raise ValueError("multiple variable-width quantifiers are not allowed")
        index += 1


def compile_expect_regex(
    expect_regex: str | None,
    *,
    flags: int = 0,
    max_length: int = MAX_EXPECT_REGEX_LEN,
) -> re.Pattern[str] | None:
    """Compile prompt guards without importing the FastAPI server hub."""
    if not expect_regex:
        return None
    if len(expect_regex) > max_length:
        raise PromptRegexError("expect_regex too long", kind="too_long", max_length=max_length)
    try:
        _validate_pattern_safety(expect_regex)
    except ValueError as exc:
        raise PromptRegexError(f"unsafe expect_regex: {exc}", kind="unsafe", max_length=max_length) from exc
    try:
        return re.compile(expect_regex, flags)
    except re.error as exc:
        raise PromptRegexError(f"invalid expect_regex: {exc}", kind="invalid", max_length=max_length) from exc


def _mono_to_wall(mono: float | None) -> float | None:
    """Convert a monotonic timestamp to wall-clock for API responses."""
    if mono is None:
        return None
    return mono + (time.time() - time.monotonic())


# Matches /hijack/{hijack_id}/ in any path segment position.
_HIJACK_ID_RE = re.compile(r"/hijack/([0-9a-fA-F\-]{1,64})/")
_MIN_LEASE_S = 1
_MAX_LEASE_S = 3600
_MAX_INPUT_CHARS = 10_000  # must match main package TermHub.max_input_chars default
_MAX_TIMEOUT_MS = 30_000
_MAX_PROMPT_POLL_S = 30.0
_MAX_REGEX_LEN = MAX_EXPECT_REGEX_LEN


def _safe_int(val: object, default: int, *, min_val: int | None = None, max_val: int | None = None) -> int:
    """Coerce *val* to ``int``, returning *default* on failure or ``None``."""
    raw: Any = default if val is None else val
    try:
        result = int(raw)
    except (ValueError, TypeError):
        return default
    if min_val is not None:
        result = max(result, min_val)
    if max_val is not None:
        result = min(result, max_val)
    return result


def _extract_hijack_id(path: str) -> str | None:
    m = _HIJACK_ID_RE.search(path)
    return m.group(1) if m else None


def _parse_lease_s(payload: dict[str, object], *, default: int = 60) -> tuple[int | None, str | None]:
    value: Any = payload.get("lease_s", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, "lease_s must be an integer"
    return max(_MIN_LEASE_S, min(parsed, _MAX_LEASE_S)), None


def _extract_prompt_id(snapshot: dict[str, object] | None) -> str | None:
    return extract_prompt_id(snapshot)


async def _wait_for_prompt(
    runtime: RuntimeProtocol,
    *,
    expect_prompt_id: str | None,
    expect_regex: re.Pattern[str] | None,
    timeout_ms: int,
    poll_interval_ms: int,
) -> dict[str, object] | None:
    """Poll last_snapshot until a prompt guard matches or the timeout expires.

    ``expect_regex`` must be a pre-compiled pattern (or None) — callers are
    responsible for compilation so that ``re.error`` is raised before the poll
    loop begins, enabling a clean 400 response to the client.
    """
    timeout_s = max(0.1, min(timeout_ms / 1000, _MAX_PROMPT_POLL_S))
    interval_s = max(0.05, min(poll_interval_ms / 1000, 5.0))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = runtime.last_snapshot
        if snapshot_matches(snapshot, expect_prompt_id=expect_prompt_id, expect_regex=expect_regex):
            return cast("dict[str, object] | None", snapshot)
        await asyncio.sleep(interval_s)
    return cast("dict[str, object] | None", runtime.last_snapshot)


async def _wait_for_analysis(runtime: RuntimeProtocol, *, timeout_ms: int = 5_000) -> str | None:
    """Poll last_analysis until a result arrives or the timeout expires."""
    timeout_s = max(0.1, min(timeout_ms / 1000, _MAX_PROMPT_POLL_S))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if runtime.last_analysis:
            return cast("str | None", runtime.last_analysis)
        await asyncio.sleep(0.2)
    return cast("str | None", runtime.last_analysis)


def _session_status_item(runtime: RuntimeProtocol) -> dict[str, object]:
    """Build a SessionStatus-compatible dict from the current DO state."""
    connected = runtime.worker_ws is not None or (
        getattr(runtime, "_ushell", None) is not None and bool(getattr(runtime, "_ushell_started", False))
    )
    meta = runtime.meta
    return {
        "session_id": runtime.worker_id,
        "display_name": meta.get("display_name") or runtime.worker_id,
        "created_at": meta.get("created_at") or 0.0,
        "connector_type": meta.get("connector_type") or "unknown",
        "lifecycle_state": runtime.lifecycle_state,
        "input_mode": runtime.input_mode,
        "connected": connected,
        "auto_start": False,
        "tags": meta.get("tags") or [],
        "recording_enabled": True,
        "recording_available": runtime.store.current_event_seq(runtime.worker_id) > 0,
        "owner": meta.get("owner"),
        "visibility": meta.get("visibility") or "public",
        "last_error": None,
        "hijacked": runtime.hijack.session is not None,
    }
