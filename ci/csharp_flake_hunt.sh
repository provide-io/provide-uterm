#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Measure how often a single C# test fails, by running it many times against a
# prebuilt assembly. A timing-sensitive test that fails roughly once per twenty
# CI runs cannot be judged by one green run, and a fix for one cannot be judged
# by one green run either -- both arms need a rate, not an outcome.
#
# ARM=baseline restores the named files from BASE_SHA first, so the same
# workflow measures the unfixed code and the fixed code under one runner image.
#
# Usage: ARM=baseline|fixed BASE_SHA=<sha> ITERATIONS=<n> FILTER=<xunit-filter> \
#          ci/csharp_flake_hunt.sh
set -euo pipefail

ARM="${ARM:-fixed}"
ITERATIONS="${ITERATIONS:-200}"
FILTER="${FILTER:?FILTER is required}"
PROJECT="packages/provide-uterm-csharp/tests/Provide.Uterm.Tests"
# The arm under study lives entirely in these two files: the test's own
# constants and the hub double it drives.
RESTORE=("$PROJECT/FanoutExecutionTests.cs" "$PROJECT/FanoutExecutionTests.TestDoubles.cs")
# Usable outside Actions, where neither of these is set.
RUNNER_TEMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
GITHUB_STEP_SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ "$ARM" = "baseline" ]; then
  BASE_SHA="${BASE_SHA:?BASE_SHA is required for the baseline arm}"
  echo "restoring ${#RESTORE[@]} file(s) from $BASE_SHA"
  git checkout "$BASE_SHA" -- "${RESTORE[@]}"
fi

# Build once. Release matches what `make cover` runs in the quality gate, which
# is where the failure was observed.
dotnet build "$PROJECT" -c Release --nologo -v quiet

failures=0
# Arithmetic loop rather than seq, which Git Bash on the Windows runner
# does not reliably provide.
for ((i = 1; i <= ITERATIONS; i++)); do
  if ! dotnet test "$PROJECT" -c Release --no-build --nologo -v quiet \
      --filter "$FILTER" > "$RUNNER_TEMP/iteration.log" 2>&1; then
    failures=$((failures + 1))
    echo "--- iteration $i FAILED (total $failures) ---"
    grep -E "Assert|Found:|Collection:|Failed " "$RUNNER_TEMP/iteration.log" | head -6 || true
  fi
done

echo "ARM=$ARM ITERATIONS=$ITERATIONS FAILURES=$failures"
echo "### \`$ARM\`: $failures / $ITERATIONS failed" >> "$GITHUB_STEP_SUMMARY"
# The script reports a rate; it must not fail the job when the baseline flakes,
# because a flaking baseline is the result being measured.
