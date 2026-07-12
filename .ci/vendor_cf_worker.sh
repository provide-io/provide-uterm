#!/usr/bin/env bash
# Build the Cloudflare Worker's Pyodide vendor tree (python_modules/) as a FLAT,
# importable package tree. wrangler bundles python_modules/ verbatim into the
# worker, and the Pyodide runtime must be able to import every pure-Python module
# the worker loads at startup:
#   - structlog          (transitive dependency of provide.telemetry)
#   - provide.telemetry  (eagerly imported by do/session_runtime/fetch.py)
#   - provide.uterm.*    (bridge, control_channel, tunnel, server, ... pulled in
#                         from the core + server + client workspace packages)
#
# The cloudflare package itself is NOT vendored here — it ships from src/ via
# wrangler.toml `main = src/worker_entry.py`, whose directory (the package src/
# root) wrangler bundles so the `provide/uterm/cloudflare/` tree is preserved and
# the worker's `from provide.uterm.cloudflare.X import Y` imports resolve as-is.
#
# NOTE: `pywrangler sync` produces a virtualenv-style python_modules/ layout that
# the Pyodide worker cannot import; this script builds the flat tree directly.
# python_modules/ is .gitignore'd, so re-run this before `wrangler deploy`.
#
# Usage (from anywhere):  bash .ci/vendor_cf_worker.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CF="$ROOT/packages/provide-uterm-cloudflare"
VENDOR="$CF/python_modules"

# Locate the installed pure-Python deps (resolved from the active env, so this is
# robust to the interpreter's site-packages path / Python version).
STRUCTLOG="$(python3 -c 'import os, structlog; print(os.path.dirname(structlog.__file__))')"
TELEMETRY="$(python3 -c 'import os, provide.telemetry as m; print(os.path.dirname(m.__file__))')"

rm -rf "$VENDOR"
mkdir -p "$VENDOR/provide/uterm"

cp -R "$STRUCTLOG" "$VENDOR/structlog"
cp -R "$TELEMETRY" "$VENDOR/provide/telemetry"

# Overlay the provide.uterm.* tree: core first (it owns the shared __init__/
# modules), then server and client with -n so core's files win on any overlap.
cp -R "$ROOT/packages/provide-uterm/src/provide/uterm/." "$VENDOR/provide/uterm/"
cp -Rn "$ROOT/packages/provide-uterm-server/src/provide/uterm/." "$VENDOR/provide/uterm/" 2>/dev/null || true
cp -Rn "$ROOT/packages/provide-uterm-client/src/provide/uterm/." "$VENDOR/provide/uterm/" 2>/dev/null || true

# The full package __init__ eagerly imports pydantic-backed builders and heavy
# ANSI/render helpers. Pyodide on Workers has no pydantic_core binary, so that
# import crashes the DO before any route runs. The worker only needs submodules
# (bridge, control_channel, tunnel, shell, …) via explicit imports — replace
# with a namespace-style init that does not pull optional heavy deps.
cat >"$VENDOR/provide/uterm/__init__.py" <<'PY'
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CF Worker vendor shim for provide.uterm (no eager pydantic imports)."""
from __future__ import annotations

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)
__version__ = "0.0.0-cf-vendor"
PY

# Ensure provide/ is a namespace package root if missing.
if [ ! -f "$VENDOR/provide/__init__.py" ]; then
  printf '%s\n' '# namespace' >"$VENDOR/provide/__init__.py"
fi

# Pyodide resolves provide.uterm from python_modules first and does not always
# merge the worker src tree as a second path. Overlay the cloudflare package
# from src so ``provide.uterm.cloudflare`` resolves under the same package root.
CF_SRC="$CF/src/provide/uterm/cloudflare"
if [ -d "$CF_SRC" ]; then
  rm -rf "$VENDOR/provide/uterm/cloudflare"
  cp -R "$CF_SRC" "$VENDOR/provide/uterm/cloudflare"
fi

# Strip bytecode caches to keep the uploaded bundle lean.
find "$VENDOR" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "CF Worker vendor tree built: $VENDOR"
# Smoke: required submodules for DO + tunnel share paths.
for sub in provide/telemetry provide/uterm/bridge provide/uterm/tunnel provide/uterm/control_channel.py provide/uterm/cloudflare; do
  if [ ! -e "$VENDOR/$sub" ]; then
    echo "ERROR: missing $VENDOR/$sub after vendor build" >&2
    exit 1
  fi
done
echo "vendor smoke ok (telemetry, bridge, tunnel, control_channel, cloudflare)"
