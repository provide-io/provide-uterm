#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run the live interop matrix: every client language against every server
# language, over real sockets on ephemeral ports.
#
# This is Layer B. The offline corpora under each package are Layer A: they
# prove each port decides the same things and prove nothing about wiring.
#
# Every driver that is not built is printed by --list-drivers before the run,
# so a short matrix is visible rather than implied. The run fails if any cell
# failed or errored; a cell that reported an unsupported capability does not
# fail it, but is printed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONPATH="${REPO_ROOT}/conformance/live${PYTHONPATH:+:${PYTHONPATH}}"

server_impl="${SERVER_IMPL:-}"
if [ -n "$server_impl" ]; then
  echo "=== drivers ==="
  uv run python conformance/live/harness --servers "$server_impl" --list-drivers
  echo
  echo "=== matrix ==="
  uv run python conformance/live/harness --servers "$server_impl" "$@"
else
  echo "=== drivers ==="
  uv run python conformance/live/harness --list-drivers
  echo
  echo "=== matrix ==="
  uv run python conformance/live/harness "$@"
fi
