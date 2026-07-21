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
#   - Core and Cloudflare are strict mypy gates. The remaining packages still
#     surface legitimate (or near-legitimate) errors that CI tracks in soft
#     mode until their package-specific type debt is retired.
#   - ty (Astral's experimental type-checker) is kept informational while it
#     matures, but the repository is expected to stay diagnostic-clean.
set -euo pipefail

STRICT_PACKAGES=(
  "packages/provide-uterm/src/"
  "packages/provide-uterm-cloudflare/src/"
  "packages/provide-uterm-annotation/src/"
  "packages/provide-uterm-server/src/"
  "packages/provide-uterm-client/src/"
  "packages/provide-uterm-platform/src/"
)

SOFT_PACKAGES=(
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
    # Informational only — never fail the gate. Empty SOFT_PACKAGES must not
    # trip `set -u` on "${SOFT_PACKAGES[@]}" (bash unbound-variable).
    rc=0
    packages=("${STRICT_PACKAGES[@]}")
    if [ "${#SOFT_PACKAGES[@]}" -gt 0 ]; then
      packages+=("${SOFT_PACKAGES[@]}")
    fi
    for pkg in "${packages[@]}"; do
      echo "::group::ty ${pkg}"
      uv run ty check "${pkg}" || rc=$?
      echo "::endgroup::"
    done
    if [ "${rc}" -ne 0 ]; then
      echo "::warning::ty reported issues (informational only)"
    fi
    exit 0
    ;;
  *)
    echo "usage: $0 {mypy|mypy-soft|ty}" >&2
    exit 2
    ;;
esac
