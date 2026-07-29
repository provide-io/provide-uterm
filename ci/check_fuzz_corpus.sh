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

CORPUS="conformance/fuzz/control_channel_fuzz.json"
GENERATOR="conformance/fuzz/gen_control_channel_fuzz.py"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

committed="$(shasum -a 256 "$CORPUS" | cut -d' ' -f1)"

# Two independent generations from the default (committed) seed.
uv run python "$GENERATOR" --out "$scratch/a.json" >/dev/null || exit 1
uv run python "$GENERATOR" --out "$scratch/b.json" >/dev/null || exit 1

first="$(shasum -a 256 "$scratch/a.json" | cut -d' ' -f1)"
second="$(shasum -a 256 "$scratch/b.json" | cut -d' ' -f1)"

if [[ "$first" != "$second" ]]; then
  echo "FAIL: $GENERATOR is not deterministic — two runs from the same seed differ."
  diff "$scratch/a.json" "$scratch/b.json" | head -20
  exit 1
fi

if [[ "$first" != "$committed" ]]; then
  echo "FAIL: $CORPUS is stale — the reference no longer produces what is recorded."
  cp "$scratch/a.json" "$CORPUS"
  echo "  The regenerated corpus has been written to the working tree; \`git diff\` shows the drift."
  echo "  If the change is intended, review the diff and commit it."
  exit 1
fi

echo "OK: $CORPUS is reproducible from its seed and matches the CPython reference."
