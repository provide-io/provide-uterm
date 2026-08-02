#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Compute the right --changed-only arguments for the C# mutation gate based on the
# GitHub Actions event that triggered the workflow, then exec it.
#
# Why changed-only: a full Stryker run pays a fixed ~10-20 minute whole-project
# instrumentation cost regardless of how narrow the `mutate` glob is, because C#
# compiles per-assembly. The perimeter is six files, so most pushes touch none of
# it and the entire run can be skipped.
#
# Mirrors ci/prepare_mutation_args.sh (the Python mutmut equivalent). Inputs come
# from the workflow environment:
#   GITHUB_EVENT_NAME    — pull_request | push | schedule | workflow_dispatch
#   GITHUB_BASE_REF      — PR base branch name (for pull_request)
#   GITHUB_EVENT_BEFORE  — previous commit SHA (for push)
set -euo pipefail

cd "$(dirname "$0")/.."

event="${GITHUB_EVENT_NAME:-}"
base_ref="${GITHUB_BASE_REF:-}"
before="${GITHUB_EVENT_BEFORE:-}"
zero_sha="0000000000000000000000000000000000000000"

# Resolve the base ref to diff against. Empty means "run the full perimeter".
case "${event}" in
  pull_request)      resolved="origin/${base_ref}" ;;
  schedule|workflow_dispatch) resolved="HEAD~1" ;;
  push)
    if [ -n "${before}" ] && [ "${before}" != "${zero_sha}" ]; then
      resolved="${before}"
    else
      resolved=""
    fi
    ;;
  *) resolved="" ;;
esac

args=""
if [ -n "${resolved}" ]; then
  args="--changed-only --base-ref ${resolved}"
fi

# Delegate to the make target so the gate's stages stay defined in one place
# (ci/mutation_gate.py, then `dotnet tool restore`, then ci/stryker_gate.py) and
# both receive the same --changed-only/--base-ref narrowing.
exec make mutation-gate GATE_ARGS="${args}"
