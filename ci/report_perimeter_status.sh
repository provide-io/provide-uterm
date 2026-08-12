#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Surface the LAST full-perimeter mutation result in this run's job summary.
#
# Why this exists: the weekly mutation-full cron was red for nine consecutive
# weeks (2026-06-14 .. 08-09) and nobody read it. It was never silent — it named
# the routes/ regression precisely on 08-02 — it was simply a scheduled run, and
# a scheduled run has no audience. Worse, once it is permanently red a NEWLY red
# file inside it is indistinguishable from the standing failure.
#
# The fix is not to gate on it. A weekly post-merge run has nothing to block,
# and making it a required check would hold main red until someone fixed it —
# which is precisely the state that caused the blindness. The fix is to give it
# an audience: whoever just pushed is looking at THIS run, can act, and now sees
# a one-line standing-status note when the perimeter is red.
#
# Deliberately advisory:
#   - exits 0 always; it must never turn a push red for a pre-existing condition
#   - prints NOTHING when the last full run was green, so it is not noise
#   - read-only (actions: read); it files nothing and closes nothing, keeping
#     c59cd4ac's decision that tracking for this repo lives outside the tracker
#
# Inputs from the workflow environment: GITHUB_STEP_SUMMARY, GH_TOKEN,
# GITHUB_REPOSITORY.
set -uo pipefail

WORKFLOW="mutation-full.yml"

# Never take the build down: this is a status echo, not a check. Any failure to
# reach the API is reported to the log and shrugged off.
runs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}/runs?status=completed&per_page=1" 2>/dev/null) || {
  echo "could not read ${WORKFLOW} runs; skipping perimeter status note"
  exit 0
}

conclusion=$(echo "${runs}" | jq -r '.workflow_runs[0].conclusion // empty')
url=$(echo "${runs}" | jq -r '.workflow_runs[0].html_url // empty')
started=$(echo "${runs}" | jq -r '.workflow_runs[0].created_at // empty' | cut -c1-10)
run_id=$(echo "${runs}" | jq -r '.workflow_runs[0].id // empty')

if [ -z "${conclusion}" ]; then
  echo "no completed ${WORKFLOW} run found; nothing to report"
  exit 0
fi

if [ "${conclusion}" = "success" ]; then
  echo "full perimeter green as of ${started} (${url})"
  exit 0
fi

# Red: name the legs that failed inside the gate step, the same distinction
# ci/report_mutation_full_failure.sh draws — a checkout fault is not drift.
legs=$(gh run view "${run_id}" --json jobs --jq '
  .jobs[]
  | select(.conclusion=="failure")
  | select([.steps[]? | select(.conclusion=="failure") | .name]
           | any(startswith("Mutation gate")))
  | "- \(.name)"' 2>/dev/null)

{
  echo "## ⚠️ Full mutation perimeter is red (as of ${started})"
  echo
  if [ -n "${legs}" ]; then
    echo "Legs failing in the gate step — surviving mutants, or allowlist entries"
    echo "that no longer match:"
    echo
    echo "${legs}"
  else
    echo "The run failed outside the gate step (checkout, setup, or a cancelled"
    echo "runner), so it is not evidence of perimeter drift — but it also means"
    echo "the perimeter has not actually been measured since."
  fi
  echo
  echo "Run: ${url}"
  echo
  echo "This note is advisory and does not affect this build. It exists because a"
  echo "weekly scheduled run has no reader; you are one."
} >> "${GITHUB_STEP_SUMMARY}"

echo "reported red perimeter (${conclusion}) from ${started}"
exit 0
