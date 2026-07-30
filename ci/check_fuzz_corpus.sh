#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Gate the committed cross-language fuzz corpus on two properties:
#
#   1. REPRODUCIBLE — generating twice from the committed seed produces
#      byte-identical files. The whole contract rests on this: four ports are
#      only held to identical inputs if the seed alone determines the corpus.
#      A generator that picked up ambient state (hash randomization, a set
#      iteration order, a clock) would fail here.
#
#   2. NOT STALE — the regenerated corpus matches what is committed. If the
#      reference codec changed behaviour and the corpus was not regenerated,
#      the Go, C# and TypeScript ports would keep asserting against a recording
#      of the OLD behaviour and stay green while diverging from Python. This
#      turns that into a red build on the commit that caused it.
#
# On failure the working tree is left with the regenerated corpus in place, so
# `git diff` shows exactly what moved.
#
# Run from anywhere: `bash ci/check_fuzz_corpus.sh`.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

status=0

# Every committed corpus, checked the same two ways. Add a pair here when a new
# surface gets one.
check_corpus() {
  local corpus="$1" generator="$2" name
  name="$(basename "$corpus")"

  local committed first second
  committed="$(shasum -a 256 "$corpus" | cut -d' ' -f1)"

  # Two independent generations from the default (committed) seed.
  uv run python "$generator" --out "$scratch/$name.a" >/dev/null || return 1
  uv run python "$generator" --out "$scratch/$name.b" >/dev/null || return 1

  first="$(shasum -a 256 "$scratch/$name.a" | cut -d' ' -f1)"
  second="$(shasum -a 256 "$scratch/$name.b" | cut -d' ' -f1)"

  if [[ "$first" != "$second" ]]; then
    echo "FAIL: $generator is not deterministic — two runs from the same seed differ."
    diff "$scratch/$name.a" "$scratch/$name.b" | head -20
    return 1
  fi

  if [[ "$first" != "$committed" ]]; then
    echo "FAIL: $corpus is stale — the reference no longer produces what is recorded."
    cp "$scratch/$name.a" "$corpus"
    echo "  The regenerated corpus has been written to the working tree; \`git diff\` shows the drift."
    echo "  If the change is intended, review the diff and commit it."
    return 1
  fi

  echo "OK: $corpus is reproducible from its seed and matches the CPython reference."
  return 0
}

check_corpus "conformance/fuzz/control_channel_fuzz.json" "conformance/fuzz/gen_control_channel_fuzz.py" || status=1
check_corpus "conformance/fuzz/ansi_emulator_fuzz.json" "conformance/fuzz/gen_ansi_emulator_fuzz.py" || status=1

exit "$status"
