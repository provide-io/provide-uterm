#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Open (or update) a single tracking issue when the weekly full-perimeter
# mutation run has failing files.
#
# Why this exists: the cron was never silent — it caught the routes/ regression
# on 2026-08-02 and named the exact path. It had simply been red for nine
# consecutive weeks by then (06-14 onward, a different set of files each time),
# and once a scheduled workflow is permanently red a NEWLY red file inside it is
# invisible. A failing run that nobody reads is not a gate. This turns each red
# run into one issue that names the failing paths, and closes it when the run
# goes green, so "the cron is red" becomes a state with an owner rather than
# background noise.
#
# One issue, reused: the run rewrites the body rather than commenting, so the
# issue always shows the CURRENT failing set instead of accumulating a thread
# nobody reads either.
#
# Inputs from the workflow environment:
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SERVER_URL, GH_TOKEN
set -euo pipefail

TITLE="🧬 Full-perimeter mutation gate is failing"
LABEL="mutation-gate"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# The failing matrix legs name their perimeter file in the job name, e.g.
# "mutation-gate-full (src/provide/uterm/server/routes/)".
failing=$(gh run view "${GITHUB_RUN_ID}" --json jobs \
  --jq '.jobs[] | select(.conclusion=="failure") | .name' | sed 's/^/  - /')

# A run can fail for reasons other than a mutant surviving (a killed job, a
# setup error). Report whatever failed rather than asserting a cause.
if [ -z "${failing}" ]; then
  failing="  - (no failing job names reported; see the run)"
fi

body=$(printf '%s\n\n%s\n\n%s\n%s\n\n%s\n' \
  "The weekly full-perimeter mutation run failed. Failing legs:" \
  "${failing}" \
  "Run: ${RUN_URL}" \
  "Perimeter and per-file obstacle notes: \`[tool.mutmut]\` in the root \`pyproject.toml\`." \
  "This issue is rewritten by each red run and closed automatically when the run goes green.")

# Reuse the existing open issue so weekly runs do not pile up duplicates.
existing=$(gh issue list --state open --label "${LABEL}" --search "${TITLE} in:title" \
  --json number --jq '.[0].number // empty')

if [ -n "${existing}" ]; then
  gh issue edit "${existing}" --body "${body}"
  echo "updated issue #${existing}"
else
  gh label create "${LABEL}" --description "Mutation perimeter drift" --color B60205 2>/dev/null || true
  gh issue create --title "${TITLE}" --label "${LABEL}" --body "${body}"
fi
