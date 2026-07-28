#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Re-run every differential golden generator for the TypeScript port and fail
# if a committed corpus no longer matches what the CPython reference produces.
#
# Without this the corpora drift silently: a change to a reference module
# would leave the TypeScript tests passing against a stale recording of the
# old behaviour, which is exactly the failure the corpora exist to prevent.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

testdata="packages/provide-uterm-ts/testdata"
stale=()

for generator in "$testdata"/gen_*_golden.py; do
  # gen_<name>_golden.py writes <name>_golden.json beside itself.
  name="$(basename "$generator")"
  name="${name#gen_}"
  name="${name%.py}"
  corpus="$testdata/$name.json"

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

  if [[ "$before" != "$after" ]]; then
    echo "stale golden corpus: $corpus"
    echo "  regenerate with: ${run[*]}"
    stale+=("$corpus")
  fi
done

if (( ${#stale[@]} > 0 )); then
  echo
  echo "FAIL: ${#stale[@]} TypeScript golden corpus file(s) do not match the CPython reference."
  exit 1
fi

echo "OK: every TypeScript golden corpus matches the CPython reference."
