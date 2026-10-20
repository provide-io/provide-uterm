#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Decide which perimeter files the Python mutation gate has to run for the
# GitHub Actions event that triggered the workflow, and hand them to the
# per-target matrix as a JSON array (step output `paths`).
#
# The gate used to run here, over every changed perimeter file in one job. A
# big change made that one job carry all of them end to end, and PR #165 hit
# the 90-minute cap with no verdict at all. Its log was empty too: this script
# ran the gate inside out="$(...)" to grep for the marker, which holds every
# byte until the gate exits, and a cancelled job never does. Here the gate now
# only lists targets, which takes seconds. ci.yml fans the list this
# prints across .github/workflows/mutation-shards.yml -- the same per-target
# job mutation-full.yml runs -- so each file gets its own job and its own
# timeout, and a slow file can no longer starve the others of a verdict.
#
# A support-file change (the allowlist, [tool.mutmut], the gate's scripts and
# workflow jobs; scripts/mutation_gate_config.py lists them) makes the question
# perimeter-wide, because the tooling moved and no earlier result carries over.
# The gate says so by printing MUTATION_FULL_PERIMETER_REQUIRED, and the list
# becomes the whole perimeter. That used to be deferred: the PR passed, and the
# push that landed it dispatched mutation-full.yml, so a broken perimeter was
# found only after it was on main. With one job per target the PR can answer it
# itself, so it does.
#
# Every changed path is classified (scripts/mutation_change_scope.py), deny
# by default: a perimeter source runs its own target; a path on the short,
# commented allowlist of things no Python test can read runs nothing; anything
# else -- a support file, a test, a conftest, test data, off-perimeter source,
# a dependency change -- runs the whole perimeter, sharded. Narrowing tests and
# helpers to the targets they reach is a planned follow-up: every static
# reading of that tried so far had a hole that produced an empty list, and an
# empty list passes.
#
# Inputs come from the workflow environment:
#   GITHUB_EVENT_NAME           — pull_request | push | schedule | workflow_dispatch
#   GITHUB_BASE_REF             — PR base branch name (for pull_request)
#   GITHUB_EVENT_BEFORE         — previous commit SHA (for push)
#   GITHUB_OUTPUT               — where the `paths` output is written (optional
#                                 locally: the array is printed either way)
#
# Usage in workflow:
#   - id: targets
#     run: ci/prepare_mutation_args.sh
set -euo pipefail
# A failure inside $(...) must fail the script too. Without this, a gate that
# crashed while computing the list would leave an empty one behind, and an
# empty list reads as "nothing to mutate" -- a pass.
shopt -s inherit_errexit

MARKER="MUTATION_FULL_PERIMETER_REQUIRED"
TARGETS_PREFIX="MUTATION_TARGETS="

# The whole perimeter, from the same generator mutation-full.yml uses, so the
# two can never disagree about what "the perimeter" is.
full_perimeter() {
  local paths
  paths="$(python3 ci/mutation_full_matrix.py)"
  # An empty perimeter means the generator lost the list (a renamed key, a
  # broken pyproject), not that there is nothing to check. Passing it on would
  # skip every shard and pass the gate.
  if [ -z "${paths}" ] || [ "${paths}" = "[]" ]; then
    echo "ERROR: ci/mutation_full_matrix.py returned an empty perimeter (${paths:-<nothing>})" >&2
    return 1
  fi
  printf '%s\n' "${paths}"
}

# Ask the gate what this diff touched against the base ref, without running
# mutmut. Its output goes to stderr so the log shows the
# reasoning (including which file widened the run), and the array is read from
# the one line that carries it. A gate that could not diff (an unfetchable base)
# exits non-zero and prints no array, and either one fails this step.
changed_targets() {
  local out line
  out="$(python3 scripts/run_mutation_gate.py --changed-only --emit-targets --base-ref "$1")"
  printf '%s\n' "${out}" >&2
  if printf '%s\n' "${out}" | grep -qx "${MARKER}"; then
    full_perimeter
    return
  fi
  line="$(printf '%s\n' "${out}" | grep "^${TARGETS_PREFIX}" || true)"
  if [ -z "${line}" ]; then
    echo "ERROR: run_mutation_gate.py --emit-targets printed no ${TARGETS_PREFIX} line" >&2
    return 1
  fi
  printf '%s\n' "${line#"${TARGETS_PREFIX}"}"
}

event="${GITHUB_EVENT_NAME:-}"
base_ref="${GITHUB_BASE_REF:-}"
before="${GITHUB_EVENT_BEFORE:-}"
zero_sha="0000000000000000000000000000000000000000"

case "${event}" in
  pull_request)
    paths="$(changed_targets "origin/${base_ref}")"
    ;;
  push)
    # A push that creates a branch has no `before` to diff against, so there is
    # no changed set to narrow to: the whole perimeter is the honest answer.
    if [ -n "${before}" ] && [ "${before}" != "${zero_sha}" ]; then
      paths="$(changed_targets "${before}")"
    else
      paths="$(full_perimeter)"
    fi
    ;;
  schedule|workflow_dispatch)
    paths="$(changed_targets "HEAD~1")"
    ;;
  *)
    paths="$(full_perimeter)"
    ;;
esac

echo "mutation targets for ${event:-<no event>}: ${paths}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "paths=${paths}" >> "${GITHUB_OUTPUT}"
fi
