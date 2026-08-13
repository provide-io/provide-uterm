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
#
# Split them by WHICH STEP failed. A leg that died in checkout or setup says
# nothing about the perimeter, and calling it drift is how a reporter starts
# crying wolf: on 2026-08-11 a runner TLS fault ("server certificate
# verification failed") took out a leg during `actions/checkout`, and a
# Stryker leg reported a mutant `Timeout` purely because the machine was
# loaded. Only a failure in the gate step itself is evidence about mutants.
jobs_json=$(gh run view "${GITHUB_RUN_ID}" --json jobs)

drift=$(echo "${jobs_json}" | jq -r '
  .jobs[]
  | select(.conclusion=="failure")
  | select([.steps[]? | select(.conclusion=="failure") | .name]
           | any(startswith("Mutation gate")))
  | "- \(.name)"')

infra=$(echo "${jobs_json}" | jq -r '
  .jobs[]
  | select(.conclusion=="failure")
  | select([.steps[]? | select(.conclusion=="failure") | .name]
           | any(startswith("Mutation gate")) | not)
  | "- \(.name) — failed in: \([.steps[]? | select(.conclusion=="failure") | .name] | join(", "))"')

{
  if [ -n "${drift}" ]; then
    echo "## Mutation perimeter: drift detected"
    echo
    echo "These legs failed IN the gate step — a surviving mutant, or a mutant"
    echo "the allowlist no longer matches:"
    echo
    echo "${drift}"
    echo
    echo "Per-file obstacle notes live in \`[tool.mutmut]\` in the root \`pyproject.toml\`."
  fi

  if [ -n "${infra}" ]; then
    echo
    echo "## Not drift: $(echo "${infra}" | wc -l | tr -d ' ') leg(s) failed before the gate ran"
    echo
    echo "Checkout/setup/runner failures. These say nothing about the perimeter;"
    echo "re-run them before reading anything into this run."
    echo
    echo "${infra}"
  fi

  if [ -z "${drift}${infra}" ]; then
    echo "## Mutation perimeter: run failed with no failing job named"
    echo
    echo "Nothing matched a failing job — the run itself was cancelled or timed out."
  fi
} >> "${GITHUB_STEP_SUMMARY}"

echo "drift legs: $([ -n "${drift}" ] && echo "${drift}" | wc -l | tr -d ' ' || echo 0)"
echo "infra legs: $([ -n "${infra}" ] && echo "${infra}" | wc -l | tr -d ' ' || echo 0)"
