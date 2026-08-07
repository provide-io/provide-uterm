#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Close the full-perimeter tracking issue once a run is fully green, so the
# issue's presence means "the perimeter is drifting right now" rather than
# "it drifted once". Without this the issue becomes stale and gets ignored,
# which is the failure mode this whole mechanism exists to prevent.
set -euo pipefail

TITLE="🧬 Full-perimeter mutation gate is failing"
LABEL="mutation-gate"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

existing=$(gh issue list --state open --label "${LABEL}" --search "${TITLE} in:title" \
  --json number --jq '.[0].number // empty')

if [ -n "${existing}" ]; then
  gh issue close "${existing}" --comment "Full perimeter green: ${RUN_URL}"
  echo "closed issue #${existing}"
else
  echo "no open tracking issue; nothing to close"
fi
