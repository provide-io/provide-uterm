#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Turn the sharded Python mutation gate's job results into one pass or fail,
# reported under the job name `mutation-gate` that the check has always had.
#
# ci.yml computes the changed perimeter targets in `mutation-targets`, runs one
# job per target in `mutation-shards`, and calls this from the `mutation-gate`
# job, which runs whatever happened before it. The rule is that only a positive
# answer passes:
#
#   * The target list must have been computed. If it was not, nobody knows what
#     should have run, so nothing that did run can stand for the change.
#   * An empty list passes at once: no perimeter file changed, and the shard
#     job is skipped because a matrix cannot be empty.
#   * Otherwise every shard must have succeeded. A failed shard is a surviving
#     mutant or a broken run. A cancelled one -- a timeout, or a runner lost --
#     never produced a verdict, and a skipped one never ran: both fail, because
#     "no answer" passing as "no survivors" is how a gate stops gating.
#
# Inputs from the workflow environment:
#   TARGETS_RESULT  — needs.mutation-targets.result
#   TARGET_PATHS    — needs.mutation-targets.outputs.paths (a JSON array)
#   SHARDS_RESULT   — needs.mutation-shards.result
set -euo pipefail

targets_result="${TARGETS_RESULT:-}"
paths="${TARGET_PATHS:-}"
shards_result="${SHARDS_RESULT:-}"

if [ "${targets_result}" != "success" ]; then
  echo "mutation gate: FAIL -- the changed perimeter targets were not computed (mutation-targets: ${targets_result:-<none>})"
  exit 1
fi

if [ "${paths}" = "[]" ]; then
  echo "mutation gate: pass -- no perimeter target changed, so there was nothing to mutate"
  exit 0
fi

if [ "${shards_result}" != "success" ]; then
  echo "mutation gate: FAIL -- the per-target shards did not all pass (mutation-shards: ${shards_result:-<none>})"
  echo "targets: ${paths}"
  echo "Open the failing 'mutation-shards / target (...)' job. If it failed in its 'Mutation gate:' step, that"
  echo "step names the surviving mutants. If it failed before that step (checkout, npm ci, uv sync), it is a"
  echo "setup fault that says nothing about mutants: re-run it."
  exit 1
fi

echo "mutation gate: pass -- every target killed: ${paths}"
