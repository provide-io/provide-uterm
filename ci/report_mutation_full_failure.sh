#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Name the failing perimeter files in the run's job summary.
#
# Why this exists: the weekly cron was never silent — it caught the routes/
# regression on 2026-08-02 and named the exact path. It had simply been red for
# nine consecutive weeks by then (06-14 onward, a different set of files each
# time), and once a scheduled workflow is permanently red a NEWLY red file
# inside it is invisible. Worse, the per-leg names only appear if you expand the
# matrix in the UI, so "which files are failing" cost several clicks.
#
# This writes the failing legs to the run summary, where they are the first
# thing visible on the run page. Deliberately NOT a GitHub issue: tracking lives
# in a local .provide/HANDOFF.md, not in the tracker.
#
# Inputs from the workflow environment:
#   GITHUB_RUN_ID, GITHUB_STEP_SUMMARY, GH_TOKEN
set -euo pipefail

# The failing matrix legs name their perimeter file in the job name, e.g.
# "mutation-gate-full (src/provide/uterm/server/routes/)".
failing=$(gh run view "${GITHUB_RUN_ID}" --json jobs \
  --jq '.jobs[] | select(.conclusion=="failure") | .name' | sed 's/^/- /')

# A run can fail for reasons other than a surviving mutant (a killed job, a
# setup error). Report whatever failed rather than asserting a cause.
if [ -z "${failing}" ]; then
  failing="- (no failing job names reported; see the run)"
fi

{
  echo "## Mutation perimeter: drift detected"
  echo
  echo "Failing legs:"
  echo
  echo "${failing}"
  echo
  echo "Per-file obstacle notes live in \`[tool.mutmut]\` in the root \`pyproject.toml\`."
} >> "${GITHUB_STEP_SUMMARY}"

echo "reported $(echo "${failing}" | wc -l | tr -d ' ') failing leg(s) to the run summary"
