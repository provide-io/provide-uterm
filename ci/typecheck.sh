#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run mypy and (optionally) ty across every workspace src/ tree.
#
# Modes:
#   ci/typecheck.sh mypy           — strict: fail on any mypy error
#   ci/typecheck.sh mypy-soft      — informational: log mypy errors, exit 0
#   ci/typecheck.sh ty             — informational: log ty errors, exit 0
#
# The split exists because:
#   - The core package (provide-uterm) is the only tree known to be 100%
#     clean under mypy strict mode today. The other packages still surface
#     legitimate (or near-legitimate) errors that we want CI to track but
#     not block on yet.
#   - ty (Astral's experimental type-checker) has known cross-file resolution
#     gaps (see [tool.mypy.overrides] in pyproject.toml) and reports false
#     positives across every package, so it always runs in informational mode.
set -euo pipefail

STRICT_PACKAGES=(
  "packages/provide-uterm/src/"
)

SOFT_PACKAGES=(
  "packages/provide-uterm-server/src/"
  "packages/provide-uterm-client/src/"
  "packages/provide-uterm-platform/src/"
  "packages/provide-uterm-cloudflare/src/"
)

mode="${1:-mypy}"
case "${mode}" in
  mypy)
    uv run mypy "${STRICT_PACKAGES[@]}"
    ;;
  mypy-soft)
    rc=0
    for pkg in "${SOFT_PACKAGES[@]}"; do
      echo "::group::mypy ${pkg}"
      uv run mypy "${pkg}" || rc=$?
      echo "::endgroup::"
    done
    if [ "${rc}" -ne 0 ]; then
      echo "::warning::mypy reported issues in one or more soft-mode packages (see groups above)"
    fi
    exit 0
    ;;
  ty)
    rc=0
    for pkg in "${STRICT_PACKAGES[@]}" "${SOFT_PACKAGES[@]}"; do
      echo "::group::ty ${pkg}"
      uv run ty check "${pkg}" || rc=$?
      echo "::endgroup::"
    done
    if [ "${rc}" -ne 0 ]; then
      echo "::warning::ty reported issues (known cross-file resolution gap; informational only)"
    fi
    exit 0
    ;;
  *)
    echo "usage: $0 {mypy|mypy-soft|ty}" >&2
    exit 2
    ;;
esac
