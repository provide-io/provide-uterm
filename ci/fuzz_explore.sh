#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Exploratory control-frame fuzzing: run the property checks against the CPython
# reference with FRESH random seeds, several times over.
#
# Separate from ci/check_fuzz_corpus.sh on purpose. That one proves the frozen
# corpus still holds, and must never change; this one is allowed to find
# something new. The committed corpus can only ever re-check inputs someone
# already recorded — this is where inputs nobody thought of come from.
#
# Every seed is printed before it runs, and the failing seed plus the base64 of
# the offending input are printed on divergence, so the case can be pasted into
# _REGRESSIONS in the generator and become permanent.
#
# Usage: bash ci/fuzz_explore.sh [rounds] [iterations-per-round]

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

rounds="${1:-8}"
iterations="${2:-50000}"
failures=0

echo "exploratory fuzz: ${rounds} fresh seeds x ${iterations} iterations per property"

for ((round = 1; round <= rounds; round++)); do
  # A fresh seed per round: independent draws find more than one long run of
  # the same stream, because each round re-rolls the segment mix from scratch.
  seed="$(uv run python -c 'import secrets; print(secrets.randbelow(2**31))')"
  echo
  echo "--- round ${round}/${rounds} (seed=${seed}) ---"
  if ! uv run python conformance/fuzz/explore_control_channel_fuzz.py --seed "$seed" --iterations "$iterations"; then
    failures=$((failures + 1))
  fi
done

echo
if ((failures == 0)); then
  echo "OK: ${rounds} exploratory rounds found no divergence."
  exit 0
fi

echo "FAIL: ${failures}/${rounds} exploratory rounds diverged."
echo "  Each failing round printed its seed and the offending input above."
echo "  Pin every one as a CCF-REG-nnnn case in conformance/fuzz/gen_control_channel_fuzz.py,"
echo "  regenerate the corpus, and commit — a divergence found once must never be found twice."
exit 1
