#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Compute the right --changed-only arguments for scripts/run_mutation_gate.py
# based on the GitHub Actions event that triggered the workflow, then exec it.
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

event="${GITHUB_EVENT_NAME:-}"
base_ref="${GITHUB_BASE_REF:-}"
before="${GITHUB_EVENT_BEFORE:-}"
zero_sha="0000000000000000000000000000000000000000"

case "${event}" in
  pull_request)
    exec "${GATE[@]}" --changed-only --base-ref "origin/${base_ref}"
    ;;
  push)
    if [ -n "${before}" ] && [ "${before}" != "${zero_sha}" ]; then
      exec "${GATE[@]}" --changed-only --base-ref "${before}"
    fi
    exec "${GATE[@]}"
    ;;
  schedule|workflow_dispatch)
    exec "${GATE[@]}" --changed-only --base-ref "HEAD~1"
    ;;
  *)
    exec "${GATE[@]}"
    ;;
esac
