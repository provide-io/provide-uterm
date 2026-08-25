#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Measure how often a C# test fails, by repeating the thing that failed and
# counting. A test that fails on roughly one CI run in twenty-four cannot be
# judged by one green run, and a fix for it cannot be judged by one green run
# either -- both arms need a rate, not an outcome.
#
# MODE=cover repeats `make cover-batch BATCH=2`: coverlet-instrumented and
# single-threaded, which is how `cover` runs and the only place the
# sequential-budget test has ever failed. An earlier attempt looped the single
# test uninstrumented and in isolation instead, and reported 0/400 on BOTH
# arms -- it reproduced nothing, so it measured nothing.
#
# MODE=test repeats one test on its own. Cheap, and useful for a failure that
# does not depend on what else is running.
#
# ARM=baseline restores the files under study from BASE_SHA first, so one
# runner image measures the unfixed code and the fixed code the same way.
#
# Usage: ARM=baseline|fixed MODE=cover|test BASE_SHA=<sha> ITERATIONS=<n> \
#          FILTER=<xunit-filter> ATTRIBUTE=<test-name> ci/csharp_flake_hunt.sh
set -euo pipefail

ARM="${ARM:-fixed}"
MODE="${MODE:-cover}"
ITERATIONS="${ITERATIONS:-45}"
FILTER="${FILTER:-}"
# Batch 2 runs the whole assembly, so a red iteration is not necessarily a red
# for the test under study. ATTRIBUTE is grepped out of the iteration log to
# separate "this test failed" from "something in the batch failed".
ATTRIBUTE="${ATTRIBUTE:-Sequential_Members_Share_One_Total_Response_Budget}"
CSHARP="packages/provide-uterm-csharp"
PROJECT="$CSHARP/tests/Provide.Uterm.Tests"
# The arm under study lives entirely in these two files: the test's own
# constants and the hub double it drives.
RESTORE=("$PROJECT/FanoutExecutionTests.cs" "$PROJECT/FanoutExecutionTests.TestDoubles.cs")
# Usable outside Actions, where neither of these is set.
RUNNER_TEMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
GITHUB_STEP_SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
LOG="$RUNNER_TEMP/iteration.log"

if [ "$ARM" = "baseline" ]; then
  BASE_SHA="${BASE_SHA:?BASE_SHA is required for the baseline arm}"
  echo "restoring ${#RESTORE[@]} file(s) from $BASE_SHA"
  git checkout "$BASE_SHA" -- "${RESTORE[@]}"
fi

run_iteration() {
  if [ "$MODE" = "cover" ]; then
    CI=true make -C "$CSHARP" cover-batch BATCH=2 > "$LOG" 2>&1
  else
    dotnet test "$PROJECT" -c Release --no-build --nologo -v quiet \
      --filter "${FILTER:?FILTER is required for MODE=test}" > "$LOG" 2>&1
  fi
}

# MODE=test reuses one build; MODE=cover rebuilds per iteration through make,
# which is what the real gate does.
if [ "$MODE" = "test" ]; then
  dotnet build "$PROJECT" -c Release --nologo -v quiet
fi

failures=0
attributed=0
# Arithmetic loop rather than seq, which Git Bash on the Windows runner
# does not reliably provide.
for ((i = 1; i <= ITERATIONS; i++)); do
  if run_iteration; then
    continue
  fi
  failures=$((failures + 1))
  if grep -q "$ATTRIBUTE" "$LOG"; then
    attributed=$((attributed + 1))
    echo "--- iteration $i FAILED in $ATTRIBUTE (attributed $attributed) ---"
  else
    echo "--- iteration $i FAILED elsewhere in the batch (total $failures) ---"
  fi
  grep -E "Assert|Found:|Collection:|\[FAIL\]|Failed " "$LOG" | head -8 || true
done

echo "ARM=$ARM MODE=$MODE ITERATIONS=$ITERATIONS FAILURES=$failures ATTRIBUTED=$attributed"
echo "### \`$ARM\` (${MODE}): $attributed / $ITERATIONS in $ATTRIBUTE, $failures / $ITERATIONS total" \
  >> "$GITHUB_STEP_SUMMARY"
# The script reports a rate; it must not fail the job when the baseline flakes,
# because a flaking baseline is the result being measured.
