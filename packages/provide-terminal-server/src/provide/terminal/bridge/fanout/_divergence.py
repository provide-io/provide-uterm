#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Divergence detection for fan-out sessions.

Compares outputs from N sessions that received identical input and flags
any session whose output diverges from the majority consensus.
"""

from __future__ import annotations

import difflib


def compute_divergence(outputs: list[str], *, threshold: float) -> list[bool]:
    """Return per-output divergence flags.

    Finds majority output (highest average similarity to all others via
    difflib.SequenceMatcher.ratio()), then flags each output whose ratio
    vs. majority falls below threshold.

    Returns list of bools — True means divergent.
    """
    n = len(outputs)

    if n == 0:
        return []

    if n == 1:
        return [False]

    def similarity(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio()

    # Compute average similarity of each output against all others
    avg_similarities = [sum(similarity(outputs[i], outputs[j]) for j in range(n) if j != i) / (n - 1) for i in range(n)]

    # The majority is the output with the highest average similarity to all others
    majority_idx = avg_similarities.index(max(avg_similarities))
    majority = outputs[majority_idx]

    # Compute per-output similarity to the majority
    sim_to_majority = [similarity(output, majority) for output in outputs]

    # Non-majority outputs are flagged if their similarity to the majority < threshold.
    # The majority itself is flagged only when it has no supporters — i.e., no other
    # output is within threshold of it (meaning there is no real consensus at all).
    has_supporters = any(i != majority_idx and sim_to_majority[i] >= threshold for i in range(n))

    return [not has_supporters if i == majority_idx else sim_to_majority[i] < threshold for i in range(n)]
