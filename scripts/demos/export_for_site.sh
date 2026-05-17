#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# export_for_site.sh
#
# One-shot wrapper to ensure demo artifacts exist, render posters, and emit
# the JSON catalog that site-uterm-io's sync script consumes.
#
# Re-running is safe: scripts.demos.reel only re-records demos whose artifacts
# are missing or out of date, and the manifest builder is idempotent.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if [[ "${SKIP_REEL:-0}" != "1" ]]; then
  # Record any missing demos and assemble the highlight reel.
  uv run python -m scripts.demos.reel
fi

# Emit demo/site-manifest.json and (re)generate posters for mp4 demos.
uv run python -m scripts.demos.build_site_manifest --posters

echo "demo manifest ready at ${ROOT_DIR}/demo/site-manifest.json"
