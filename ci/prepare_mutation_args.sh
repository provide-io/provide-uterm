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
    if should_dispatch; then
      dispatch_full_perimeter || return 1
    else
      echo "pull_request: the full perimeter will be dispatched when this lands on the target branch"
    fi
  fi
  return "${status}"
}

# Fan the 38 perimeter targets across mutation-full.yml's matrix.
#
# --ref takes a BRANCH OR TAG, never a commit SHA: the dispatch API resolves the
# workflow file from a ref, and a raw SHA is rejected. Passing $GITHUB_SHA looked
# right, dispatched nothing, and -- because the failure only warned -- left a
# green job with no perimeter run behind it, which is the exact shape of problem
# this whole mechanism exists to remove. The dispatched run therefore uses the
# branch head; on a push those are the same commit unless another push races in.
#
# A dispatch failure FAILS the step. The gate has already said it cannot answer
# the perimeter question here; if the handoff does not happen either, then nobody
# is answering it, and a job that reports success while nothing checks the
# perimeter is worse than the timeout this replaced -- a timeout is at least
# visible. Needs `actions: write`, granted to the mutation-gate job alone.
dispatch_full_perimeter() {
  local ref="${GITHUB_REF_NAME:-main}"
  echo "dispatching mutation-full.yml on ${ref} (the perimeter is not answerable in this job)"
  if ! gh workflow run mutation-full.yml --ref "${ref}"; then
    echo "ERROR: could not dispatch mutation-full.yml on ${ref}."
    echo "The perimeter question is unanswered: this job cannot run it and the handoff failed."
    echo "Run it manually (gh workflow run mutation-full.yml --ref ${ref}) before trusting this commit."
    return 1
  fi
}

# A fork PR's token is read-only whatever the permissions block says, so the
# dispatch cannot work there. The perimeter question is deferred to the push that
# lands the change on main, which is the ref that matters, rather than failing a
# contributor's PR for a capability their token was never going to have.
should_dispatch() {
  case "${GITHUB_EVENT_NAME:-}" in
    pull_request) return 1 ;;
    *) return 0 ;;
  esac
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
