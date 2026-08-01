#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Re-run every differential golden generator in the repository and fail if a
# committed corpus no longer matches what the CPython reference produces.
#
# Without this the corpora drift silently: a change to a reference module would
# leave a port's tests passing against a stale recording of the old behaviour,
# which is exactly the failure the corpora exist to prevent. That is not
# hypothetical — `packages/provide-uterm-go/server/testdata/egress_golden.json`
# sat outside this check and recorded `100.64.0.1` as *permitted*, so the Go
# suite faithfully enforced an SSRF hole the reference had.
#
# Supersedes check_ts_goldens.sh, which scanned only the TypeScript testdata
# directory. Any port that grows a `gen_<name>_golden.py` is picked up here with
# no further wiring — the point being that a new corpus should not have to be
# remembered into a list to be guarded.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# A golden corpus is a recording of ONE reference interpreter, and some of what
# it records is legitimately version-specific: pystr pins CPython's Unicode
# tables (15.1.0 on 3.13, 16.0.0 on 3.14), statistics.variance became exact in
# 3.12, and pathlib's class repr moved in 3.13. Running this check across a
# version matrix therefore demands a single file match four interpreters at
# once — something that cannot exist, and which left CI red on three of four
# cells. The corpora are recorded against, and checked against, this one
# version; the matrix still covers every test suite, which is what it is for.
GOLDENS_REFERENCE_PYTHON="${GOLDENS_REFERENCE_PYTHON:-3.13}"
running_python="$(uv run python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${running_python}" != "${GOLDENS_REFERENCE_PYTHON}" ]]; then
  echo "SKIP: goldens are recorded against Python ${GOLDENS_REFERENCE_PYTHON}, this is ${running_python}."
  echo "      Re-run on ${GOLDENS_REFERENCE_PYTHON}, or set GOLDENS_REFERENCE_PYTHON to re-record against another."
  exit 0
fi

stale=()
checked=0

# Every generator under any package's testdata directory. Sorted so the output
# order is stable between runs and between machines.
while IFS= read -r generator; do
  # gen_<name>_golden.py writes <name>_golden.json beside itself.
  dir="$(dirname "$generator")"
  name="$(basename "$generator")"
  name="${name#gen_}"
  name="${name%.py}"
  corpus="$dir/$name.json"

  if [[ ! -f "$corpus" ]]; then
    echo "missing corpus for $generator: $corpus"
    stale+=("$corpus")
    continue
  fi

  # A generator that drives a workspace package outside the root environment
  # declares it with a `# uv-package: <name>` marker on its own line.
  package="$(sed -n 's/^# uv-package: *//p' "$generator" | head -1)"
  if [[ -n "$package" ]]; then
    run=(uv run --package "$package" python "$generator")
  else
    run=(uv run python "$generator")
  fi

  before="$(shasum -a 256 "$corpus" | cut -d' ' -f1)"
  "${run[@]}" >/dev/null
  after="$(shasum -a 256 "$corpus" | cut -d' ' -f1)"
  checked=$((checked + 1))

  if [[ "$before" != "$after" ]]; then
    echo "stale golden corpus: $corpus"
    echo "  regenerate with: ${run[*]}"
    stale+=("$corpus")
  fi
done < <(find packages -type d -name testdata -exec find {} -maxdepth 1 -name 'gen_*_golden.py' \; | sort)

if (( ${#stale[@]} > 0 )); then
  echo
  echo "FAIL: ${#stale[@]} golden corpus file(s) do not match the CPython reference."
  exit 1
fi

echo "OK: $checked golden corpus file(s) match the CPython reference."

# Corpora with no generator cannot be re-derived, so nothing above can tell
# whether they still describe the reference. Reported rather than failed: most
# predate this check and writing eight generators is its own piece of work. The
# count is printed so it is visible when it grows, which is the moment to write
# the generator rather than years later.
ungenerated=0
while IFS= read -r corpus; do
  dir="$(dirname "$corpus")"
  name="$(basename "$corpus" .json)"
  [[ -f "$dir/gen_$name.py" ]] || ungenerated=$((ungenerated + 1))
done < <(find packages -type d -name testdata -exec find {} -maxdepth 1 -name '*_golden.json' \; | sort)

if (( ungenerated > 0 )); then
  echo "NOTE: $ungenerated committed corpus file(s) have no generator and cannot be drift-checked."
fi
