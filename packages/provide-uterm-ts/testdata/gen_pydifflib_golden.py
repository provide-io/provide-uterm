#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for CPython's ``difflib`` ratio.

``SequenceMatcher.ratio()`` decides whether a fan-out session has diverged
from its peers, so the number is a policy input, not a diagnostic. It is also
not any of the similarity measures a port would reach for by default — not
Levenshtein, not longest-common-subsequence. It is Ratcliff/Obershelp:
recursively take the longest matching block, then recurse on what is left of
either side, and report twice the matched length over the combined length.

Two behaviours make a naive reimplementation wrong in ways that only show up
on real data.

**Autojunk.** For a right-hand sequence of 200 elements or more, any element
appearing in more than 1% of positions is dropped from the index entirely and
can no longer *start* a match. On terminal output — full of spaces and
newlines — this fires constantly, and it lowers the ratio. A port without it
reports two screens as more similar than CPython does, and a divergence
threshold tuned against CPython stops firing.

**Tie-breaking.** When several blocks are equally long the earliest in `a`
wins, then the earliest in `b`. Different tie-breaks pick different blocks,
which changes what is left to recurse on and therefore the final total.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pydifflib_golden.py
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("pydifflib_golden.json")

# A prompt-shaped line, repeated, to push past the autojunk threshold.
_LONG_A = ("user@host:~$ ls -la\n" * 12)[:250]
_LONG_B = ("user@host:~$ ls -la\n" * 12)[:250].replace("ls -la", "ls -l ", 3)
_SPACES_A = " " * 150 + "done"
_SPACES_B = " " * 150 + "fail"

# The autojunk cutoff is `count > len(b) // 100 + 1`. For a b of exactly 200
# elements the limit is 3, so an element appearing exactly 3 times is kept and
# one appearing 4 times is dropped. Both sides of that boundary are recorded:
# a comparison of `>=`, or a limit without the `+1`, flips the first case from
# a real match to no match at all.
_EDGE_FILLER = "".join(chr(0x100 + i) for i in range(197))
_EDGE_KEPT_B = _EDGE_FILLER + "zzz"
_EDGE_DROPPED_B = "".join(chr(0x100 + i) for i in range(196)) + "zzzz"

# A popular element cannot *start* a match but can still lengthen one. Here a
# run of 200 spaces is dropped from the index, the match seeds on the unique
# tail, and then extends backwards across every one of them — so the block
# begins at offset zero despite nothing there being indexable.
_BACKFILL_A = " " * 200 + "unique-tail"
_BACKFILL_B = " " * 200 + "unique-fail"

# The case that isolates the backwards extension. The seed lands at b=0, so the
# recursion never explores anything to its left, and only the backwards walk
# can recover the 200 shared spaces before it.
_BACKONLY_A = "ZZ" + " " * 200 + "tail"
_BACKONLY_B = " " * 200 + "tail"

# A pair where the measure is visibly not symmetric: both sides are long
# enough for autojunk, but only one of them has an element popular enough to
# be dropped, so a match can start in one direction and not the other.
_ASYM_A = "abaabababaabbaaabbbbabbababbbabbbaaaaabbaababbbabbabaababaabaaabababbbaaaaabaaabbabbabbabaaaabababbbbaabaabbaaaabbaaabbababaabbabbbbaababaabbabbaaaabaabababbaaabbbabaabaaaaaaababbaaabbabaaa"
_ASYM_B = "babaaabbbabbaaaabbbaababbaabbbbbbaabababbabbbaaaaabbabababbbaaabbbbabbbbababbbaaabbbabbabbbabaabaabbbabbabaaabbabbaaabbbbbbbbabbbaaaaababbbbaaaaabababbaabbbabbbabbbaaababbbbbaaabbbaabbbaaaaaabaaaaaababbaaababbaaabbbbabaaabbaaabbababbababbbb"

# (name, a, b)
RATIO_CASES: list[tuple[str, str, str]] = [
    ("both empty", "", ""),
    ("one empty", "", "abc"),
    ("other empty", "abc", ""),
    ("identical", "abc", "abc"),
    ("single character, same", "a", "a"),
    ("single character, different", "a", "b"),
    ("disjoint", "abc", "xyz"),
    ("one substitution", "abcd", "abed"),
    ("one insertion", "abcd", "abxcd"),
    ("one deletion", "abcd", "acd"),
    ("transposed", "abcd", "abdc"),
    ("reversed", "abcdef", "fedcba"),
    ("repeated characters", "aaaa", "aa"),
    ("classic difflib example", "abcd", "bcde"),
    ("prefix only", "prefix-common", "prefix-different"),
    ("suffix only", "alpha-suffix", "beta-suffix"),
    ("equal-length blocks tie", "abxycd", "cdxyab"),
    ("shell prompts", "user@host:~$ ", "user@host:~# "),
    ("command output", "total 24\ndrwxr-xr-x 3 user", "total 28\ndrwxr-xr-x 4 user"),
    ("error vs success", "OK: all checks passed", "FAIL: 3 checks failed"),
    ("whitespace only", "   ", "    "),
    ("newlines", "a\nb\nc", "a\nb\nd"),
    ("unicode", "héllo wörld", "héllo würld"),
    ("emoji", "done ✅", "done ❌"),
    # Long enough for the autojunk heuristic to engage on b.
    ("long, near-identical prompts", _LONG_A, _LONG_B),
    ("long, dominated by spaces", _SPACES_A, _SPACES_B),
    ("long identical", _LONG_A, _LONG_A),
    ("exactly at the autojunk threshold", "ab" * 100, "ab" * 100),
    ("just under the autojunk threshold", "ab" * 99 + "c", "ab" * 99 + "d"),
    ("asymmetric, forwards", _ASYM_A, _ASYM_B),
    ("asymmetric, backwards", _ASYM_B, _ASYM_A),
    ("element exactly at the autojunk limit", "zzz", _EDGE_KEPT_B),
    ("element one past the autojunk limit", "zzzz", _EDGE_DROPPED_B),
    ("match extends back over dropped elements", _BACKFILL_A, _BACKFILL_A),
    ("match extends back, then diverges", _BACKFILL_A, _BACKFILL_B),
    ("backwards extension is the only way left", _BACKONLY_A, _BACKONLY_B),
]

# (name, outputs, threshold) for compute_divergence.
DIVERGENCE_CASES: list[tuple[str, list[str], float]] = [
    ("no sessions", [], 0.8),
    ("one session", ["only"], 0.8),
    ("two identical", ["same", "same"], 0.8),
    ("two different", ["yes", "no"], 0.8),
    ("three in agreement", ["ok", "ok", "ok"], 0.8),
    ("one odd one out", ["ok", "ok", "totally different"], 0.8),
    ("two against one", ["ok fine", "ok fine", "broken"], 0.8),
    ("all three different", ["alpha", "beta", "gamma"], 0.8),
    ("near misses under a strict threshold", ["result 1", "result 2", "result 3"], 0.99),
    ("near misses under a loose threshold", ["result 1", "result 2", "result 3"], 0.5),
    ("threshold of zero", ["a", "b", "c"], 0.0),
    ("threshold of one", ["a", "a", "a"], 1.0),
    ("empty outputs", ["", "", ""], 0.8),
    ("one empty among agreement", ["ok", "ok", ""], 0.8),
    ("realistic: one host failed", ["Done.\nexit 0", "Done.\nexit 0", "Error.\nexit 1"], 0.8),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    from provide.uterm.server.bridge.fanout._divergence import compute_divergence

    ratios = []
    for name, a, b in RATIO_CASES:
        matcher = difflib.SequenceMatcher(None, a, b)
        ratios.append(
            {
                "name": name,
                "a": a,
                "b": b,
                "ratio": matcher.ratio(),
                # The matching blocks are recorded too: a port can produce the
                # right total from the wrong blocks, and the blocks are what
                # the recursion actually depends on.
                "blocks": [list(block) for block in matcher.get_matching_blocks()],
            }
        )

    divergences = [
        {
            "name": name,
            "outputs": outputs,
            "threshold": threshold,
            "flags": compute_divergence(outputs, threshold=threshold),
        }
        for name, outputs, threshold in DIVERGENCE_CASES
    ]

    payload: dict[str, Any] = {
        "generator": "packages/provide-uterm-ts/testdata/gen_pydifflib_golden.py",
        "autojunk_min_length": 200,
        "ratios": ratios,
        "divergences": divergences,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(ratios)} ratio cases, {len(divergences)} divergence cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
