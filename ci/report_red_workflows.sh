#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Surface workflows that are standing red ON MAIN in this run's job summary.
#
# Why this exists: `hostile-probes (csharp)` failed on every main run from
# 2026-08-14 to 08-21 and nobody noticed for a week. Two things hid it.
#
# First, it is not part of the `CI` workflow, so a green CI check on a push says
# nothing about it, and nobody opens a separate workflow they are not failing.
#
# Second, and worse: the runs that WERE green all belonged to a long-lived
# feature branch carrying a fix that main does not have. Reading "last run:
# success" without looking at the branch is how a job that has literally never
# passed on main reads as healthy. So this deliberately asks for branch=main and
# nothing else.
#
# This is the same remedy ci/report_perimeter_status.sh applies to the weekly
# mutation cron, for the same reason: the signal was never missing, it just had
# no audience. Whoever just pushed is looking at THIS run and can act.
#
# Deliberately advisory:
#   - exits 0 always; it must never turn a push red for a pre-existing condition
#   - prints NOTHING when every watched workflow is green on main, so it is not
#     noise on a healthy repo
#   - read-only (actions: read); it files nothing and closes nothing
#
# Inputs from the workflow environment: GITHUB_STEP_SUMMARY, GH_TOKEN,
# GITHUB_REPOSITORY.
set -uo pipefail

# Workflows that gate nothing but whose redness matters. `CI` is deliberately
# absent: a push already fails on it, so echoing it back would be noise.
WATCHED="${WATCHED_WORKFLOWS:-hostile-client.yml container-scan.yml}"

red_report=""

for wf in ${WATCHED}; do
  # branch=main is the whole point; see the note above about feature-branch greens.
  runs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${wf}/runs?status=completed&branch=main&per_page=1" 2>/dev/null) || {
    echo "could not read ${wf} runs; skipping"
    continue
  }

  conclusion=$(echo "${runs}" | jq -r '.workflow_runs[0].conclusion // empty')
  url=$(echo "${runs}" | jq -r '.workflow_runs[0].html_url // empty')
  started=$(echo "${runs}" | jq -r '.workflow_runs[0].created_at // empty' | cut -c1-10)
  run_id=$(echo "${runs}" | jq -r '.workflow_runs[0].id // empty')
  name=$(echo "${runs}" | jq -r '.workflow_runs[0].name // empty')

  [ -z "${conclusion}" ] && { echo "no completed main run for ${wf}"; continue; }
  [ "${conclusion}" = "success" ] && { echo "${wf} green on main as of ${started}"; continue; }

  jobs=$(gh run view "${run_id}" --json jobs --jq '
    .jobs[] | select(.conclusion=="failure")
    | "  - \(.name): " + ([.steps[]? | select(.conclusion=="failure") | .name] | join(", "))' 2>/dev/null)

  red_report="${red_report}
### ${name:-${wf}} — red on main since at least ${started}
${jobs:-  (could not read job detail)}

Run: ${url}
"
done

[ -z "${red_report}" ] && exit 0

{
  echo "## ⚠️ Standing red on main"
  echo
  echo "These workflows are not part of this push's checks, so nothing here"
  echo "failed because of your change. They were already red on main."
  echo "${red_report}"
  echo "This note is advisory and does not affect this build."
} >> "${GITHUB_STEP_SUMMARY}"

echo "reported standing-red workflows"
exit 0
