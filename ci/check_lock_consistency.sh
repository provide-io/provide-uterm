#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Lock-consistency drift gate. Fails fast at PR time if either lockfile has
# drifted from its manifest, so a stale lock can't merge unnoticed.
#
#   - npm:  regenerate package-lock.json from package.json(s) WITHOUT installing
#           (--package-lock-only), then assert the committed lock is unchanged.
#   - uv:   `uv lock --check` verifies uv.lock is up to date vs the pyprojects
#           without writing anything.
#
# Both checks are read-only / no full install — kept minimal so the CI job
# stays fast. Run locally the same way: ci/check_lock_consistency.sh
set -euo pipefail

echo "==> npm lock consistency (package-lock.json)"
# Regenerate the lock from the manifests only; no node_modules install.
npm install --package-lock-only
# Any diff means package-lock.json drifted from package.json — fail loudly.
git diff --exit-status package-lock.json

echo "==> uv lock consistency (uv.lock)"
# Verify uv.lock matches the workspace pyprojects without rewriting it.
uv lock --check

echo "==> locks are in sync"
