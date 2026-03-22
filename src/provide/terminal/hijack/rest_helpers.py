#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Shared helper functions for hijack REST contracts and prompt guards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_EXPECT_REGEX_LEN = 200


@dataclass(frozen=True)
class PromptRegexError(ValueError):
    """Raised when a prompt regex is invalid or exceeds the allowed size."""

    message: str
    kind: str
    max_length: int | None = None

    def __str__(self) -> str:
        return self.message


def extract_prompt_id(snapshot: dict[str, Any] | None) -> str | None:
    """Pull ``prompt_id`` out of a snapshot dict (returns ``None`` if absent)."""
    if not snapshot:
        return None
    prompt = snapshot.get("prompt_detected")
    if isinstance(prompt, dict):
        value = prompt.get("prompt_id")
        if isinstance(value, str) and value:
            return value
    return None


def compile_expect_regex(
    expect_regex: str | None,
    *,
    flags: int = 0,
    max_length: int = MAX_EXPECT_REGEX_LEN,
) -> re.Pattern[str] | None:
    """Compile a prompt guard regex or raise :class:`PromptRegexError`."""
    if not expect_regex:
        return None
    if len(expect_regex) > max_length:
        raise PromptRegexError("expect_regex too long", kind="too_long", max_length=max_length)
    try:
        return re.compile(expect_regex, flags)
    except re.error as exc:  # pragma: no cover - exercised by callers
        raise PromptRegexError(f"invalid expect_regex: {exc}", kind="invalid", max_length=max_length) from exc


def snapshot_matches(
    snapshot: dict[str, Any] | None,
    *,
    expect_prompt_id: str | None,
    expect_regex: re.Pattern[str] | None,
) -> bool:
    """Return True if *snapshot* satisfies the prompt-id and/or regex guard."""
    if snapshot is None:
        return False
    if expect_prompt_id and extract_prompt_id(snapshot) != expect_prompt_id:
        return False
    return expect_regex is None or bool(expect_regex.search(str(snapshot.get("screen", ""))))


def build_hijack_snapshot_response(
    *,
    worker_id: str,
    hijack_id: str,
    snapshot: dict[str, Any] | None,
    lease_expires_at: float | None,
) -> dict[str, Any]:
    """Build the canonical hijack snapshot response payload."""
    return {
        "ok": True,
        "worker_id": worker_id,
        "hijack_id": hijack_id,
        "snapshot": snapshot,
        "prompt_id": extract_prompt_id(snapshot),
        "lease_expires_at": lease_expires_at,
    }


def build_hijack_events_response(
    *,
    worker_id: str,
    hijack_id: str,
    after_seq: int,
    latest_seq: int,
    min_event_seq: int,
    events: list[Any],
    limit: int,
    lease_expires_at: float | None,
) -> dict[str, Any]:
    """Build the canonical hijack events response payload."""
    return {
        "ok": True,
        "worker_id": worker_id,
        "hijack_id": hijack_id,
        "after_seq": after_seq,
        "latest_seq": latest_seq,
        "min_event_seq": min_event_seq,
        "has_more": len(events) >= limit,
        "events": events,
        "lease_expires_at": lease_expires_at,
    }
