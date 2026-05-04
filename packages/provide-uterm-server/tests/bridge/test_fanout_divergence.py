#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.terminal.bridge.fanout._divergence import compute_divergence


def test_all_identical_outputs_not_divergent() -> None:
    """All identical outputs should produce all False."""
    result = compute_divergence(["hello world", "hello world", "hello world"], threshold=0.9)
    assert result == [False, False, False]


def test_three_identical_one_different() -> None:
    """3 identical + 1 very different → only the different one is flagged."""
    outputs = ["hello world", "hello world", "hello world", "completely different text xyz"]
    result = compute_divergence(outputs, threshold=0.9)
    assert result == [False, False, False, True]


def test_empty_list_returns_empty() -> None:
    """Empty input → empty output."""
    result = compute_divergence([], threshold=0.9)
    assert result == []


def test_single_output_not_divergent() -> None:
    """A single output has no peers to diverge from — always False."""
    result = compute_divergence(["only one"], threshold=0.9)
    assert result == [False]


def test_all_different_high_threshold_all_divergent() -> None:
    """All completely different outputs with threshold=0.99 → all flagged."""
    outputs = ["aaaa", "bbbb", "cccc", "dddd"]
    result = compute_divergence(outputs, threshold=0.99)
    assert all(result)
    assert len(result) == 4


def test_threshold_zero_nothing_divergent() -> None:
    """threshold=0.0 means anything with ratio >= 0.0 is accepted — nothing diverges."""
    outputs = ["totally different", "nothing alike", "xyzzy foobar"]
    result = compute_divergence(outputs, threshold=0.0)
    assert result == [False, False, False]


def test_empty_string_among_nonempty_flagged_as_divergent() -> None:
    """An empty string should be flagged as divergent when others have content."""
    outputs = ["shell output here", "shell output here", "shell output here", ""]
    result = compute_divergence(outputs, threshold=0.9)
    assert result == [False, False, False, True]


def test_returns_list_of_bools() -> None:
    """Return type must be list[bool]."""
    result = compute_divergence(["a", "b"], threshold=0.5)
    assert isinstance(result, list)
    assert all(isinstance(v, bool) for v in result)


def test_majority_is_selected_correctly() -> None:
    """Majority is the output with highest average similarity to all others."""
    # "aaa" is more similar to "aab" and "aac" than "zzz" is
    outputs = ["aaa", "aab", "aac", "zzz"]
    result = compute_divergence(outputs, threshold=0.5)
    # "zzz" should diverge, the "aa*" cluster should not
    assert result[3] is True
    assert result[0] is False
    assert result[1] is False
    assert result[2] is False
