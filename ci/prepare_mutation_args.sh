#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Compute the right --changed-only arguments for scripts/run_mutation_gate.py
# based on the GitHub Actions event that triggered the workflow, run it, and
# hand a perimeter-wide question off to the workflow that can answer it.
#
# The gate cannot answer a support-file change in this job: the perimeter is 38
# files that mutmut has to be given one at a time, which is hours against a
# 90-minute cap. It prints MUTATION_FULL_PERIMETER_REQUIRED instead, and this
# script dispatches mutation-full.yml on the same SHA, where the identical 38
# targets run across a matrix. The dispatch is the answer arriving later rather
# than never -- the run it replaces only ever timed out.
#
# Inputs come from the workflow environment:
#   GITHUB_EVENT_NAME           — pull_request | push | schedule | workflow_dispatch
#   GITHUB_BASE_REF             — PR base branch name (for pull_request)
#   GITHUB_EVENT_BEFORE         — previous commit SHA (for push)
#
# Usage in workflow:
#   - run: ci/prepare_mutation_args.sh
set -euo pipefail

GATE=(uv run python scripts/run_mutation_gate.py
      --python-version 3.11 --retries 1 --min-mutation-score 100)

MARKER="MUTATION_FULL_PERIMETER_REQUIRED"

# Run the gate, echo its output, and dispatch the chunked perimeter workflow if
# the gate said this job could not answer the question. The gate's exit status is
# preserved: a dispatch is not a pass, it is a deferral of a question the gate
# already decided it had no verdict on (it exits 0 in that case).
run_gate() {
  local out status
  set +e
  out="$("$@" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${out}"
  if printf '%s' "${out}" | grep -q "${MARKER}"; then
    dispatch_full_perimeter
  fi
  return "${status}"
}

# Fan the 38 perimeter targets across mutation-full.yml's matrix on this SHA.
# Needs `actions: write`, granted to the mutation-gate job alone. A dispatch
# failure is reported but does not fail the gate: losing the deferred run is
# worth knowing about, and is not itself a perimeter regression.
dispatch_full_perimeter() {
  echo "dispatching mutation-full.yml on ${GITHUB_SHA:-HEAD} (perimeter is not answerable in this job)"
  if ! gh workflow run mutation-full.yml --ref "${GITHUB_SHA:-HEAD}"; then
    echo "WARNING: could not dispatch mutation-full.yml; run it manually on this SHA"
  fi
}

event="${GITHUB_EVENT_NAME:-}"
base_ref="${GITHUB_BASE_REF:-}"
before="${GITHUB_EVENT_BEFORE:-}"
zero_sha="0000000000000000000000000000000000000000"

case "${event}" in
  pull_request)
    run_gate "${GATE[@]}" --changed-only --base-ref "origin/${base_ref}"; exit $?
    ;;
  push)
    if [ -n "${before}" ] && [ "${before}" != "${zero_sha}" ]; then
      run_gate "${GATE[@]}" --changed-only --base-ref "${before}"; exit $?
    fi
    run_gate "${GATE[@]}"; exit $?
    ;;
  schedule|workflow_dispatch)
    run_gate "${GATE[@]}" --changed-only --base-ref "HEAD~1"; exit $?
    ;;
  *)
    run_gate "${GATE[@]}"; exit $?
    ;;
esac
