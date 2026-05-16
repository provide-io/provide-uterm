#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import re

import pytest

from provide.uterm.bridge.rest_helpers import (
    MAX_EXPECT_REGEX_LEN,
    PromptRegexError,
    build_hijack_events_response,
    build_hijack_snapshot_response,
    compile_expect_regex,
    extract_prompt_id,
    snapshot_matches,
)


def test_extract_prompt_id_returns_id() -> None:
    assert extract_prompt_id({"prompt_detected": {"prompt_id": "menu"}}) == "menu"


def test_compile_expect_regex_rejects_too_long() -> None:
    with pytest.raises(PromptRegexError, match="expect_regex too long"):
        compile_expect_regex("a" * (MAX_EXPECT_REGEX_LEN + 1))


def test_snapshot_matches_uses_prompt_and_regex() -> None:
    snap = {"screen": "login prompt", "prompt_detected": {"prompt_id": "login"}}
    pattern = re.compile("prompt")
    assert snapshot_matches(snap, expect_prompt_id="login", expect_regex=pattern)
    assert not snapshot_matches(snap, expect_prompt_id="other", expect_regex=pattern)


def test_build_hijack_snapshot_response_contains_prompt_id() -> None:
    payload = build_hijack_snapshot_response(
        worker_id="w1",
        hijack_id="h1",
        snapshot={"screen": "x", "prompt_detected": {"prompt_id": "menu"}},
        lease_expires_at=123.0,
    )
    assert payload["prompt_id"] == "menu"


def test_build_hijack_events_response_sets_has_more() -> None:
    payload = build_hijack_events_response(
        worker_id="w1",
        hijack_id="h1",
        after_seq=0,
        latest_seq=3,
        min_event_seq=1,
        events=[{"seq": 1}, {"seq": 2}],
        limit=2,
        lease_expires_at=456.0,
    )
    assert payload["has_more"] is True
