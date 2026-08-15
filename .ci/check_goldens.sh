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
  # This used to `echo SKIP; exit 0`, and the skip was worse than the drift it
  # was avoiding. On a developer machine running anything other than the
  # reference version — 3.14 is the current default — the check printed SKIP and
  # exited 0, so `make quality-gate` was green while CI's `quality (3.13)` cell
  # was red. Three corpora sat stale that way, and the staleness was hiding a
  # real bug: the Go SnapshotFrame had no fields for chunks_read/bytes_read at
  # all, so decoding a genuine snapshot failed outright, while the Go suite
  # passed against a corpus recorded before those fields existed. Nobody was
  # ignoring a warning; there was no warning to ignore.
  #
  # So instead of standing down, provision the reference interpreter and re-exec
  # under it. uv can fetch 3.13 on demand, and pointing UV_PROJECT_ENVIRONMENT
  # at a dedicated directory keeps the developer's own .venv untouched — a check
  # must not repoint the environment of the tree it is checking.
  #
  # GOLDENS_ALREADY_REEXECED guards against a loop if the provisioned
  # interpreter somehow still reports the wrong version.
  if [[ -n "${GOLDENS_ALREADY_REEXECED:-}" ]]; then
    echo "FAIL: asked for Python ${GOLDENS_REFERENCE_PYTHON} and got ${running_python}." >&2
    echo "      The goldens record one interpreter's behaviour; checking them" >&2
    echo "      against another proves nothing, so this is a failure, not a skip." >&2
    exit 1
  fi

  echo "goldens: this is Python ${running_python}; re-running under the reference ${GOLDENS_REFERENCE_PYTHON}."
  goldens_env="${GOLDENS_REFERENCE_ENV:-${repo_root}/.venv-goldens}"
  if ! UV_PROJECT_ENVIRONMENT="${goldens_env}" \
       uv sync --all-packages --all-extras --group dev \
               --python "${GOLDENS_REFERENCE_PYTHON}" --quiet; then
    echo "FAIL: could not provision Python ${GOLDENS_REFERENCE_PYTHON} to check the goldens against." >&2
    echo "      Refusing to report success without having checked anything." >&2
    exit 1
  fi
  exec env GOLDENS_ALREADY_REEXECED=1 \
           UV_PROJECT_ENVIRONMENT="${goldens_env}" \
           "${BASH_SOURCE[0]}" "$@"
fi

# Throwaway trees that CONTAIN copies of real testdata directories and must not
# be scanned. Stryker copies the whole package into .stryker-tmp/sandbox-XXXXXX
# per run, and mutmut does the same into mutants/; an interrupted run leaves
# those behind. A sandbox copy holding a stale corpus makes this check fail
# against a path that is not in the repository, and the failure is
# self-clearing — the run regenerates the sandbox's copy, so the next run
# passes. That cost a confusing red `make quality-gate` on 2026-08-09 with the
# tree clean (the sandboxes are gitignored, so `git status` showed nothing).
_prune_temp_trees() {
  find packages \
    \( -name node_modules -o -name .stryker-tmp -o -name mutants -o -name .venv \) -prune \
    -o "$@"
}

find_generators() {
  _prune_temp_trees -type d -name testdata -print |
    while IFS= read -r testdata_dir; do
      find "$testdata_dir" -maxdepth 1 -name 'gen_*_golden.py'
    done | sort
}

find_corpora() {
  _prune_temp_trees -type d -name testdata -print |
    while IFS= read -r testdata_dir; do
      find "$testdata_dir" -maxdepth 1 -name '*_golden.json'
    done | sort
}

stale=()
checked=0

# Regeneration happens IN PLACE — a generator writes its corpus beside itself,
# so the only way to know whether the committed bytes still match is to let it
# overwrite them and compare hashes. That made a failure destroy its own
# evidence: by the time anyone looked, the file on disk held the NEW content and
# the committed content was gone (in CI, with the workspace, entirely). A
# NON-deterministic generator turned that into a red that could not be
# investigated at all — `provide-uterm-ts/testdata/serverhijack_golden.json`
# intermittently records a null body for the `snapshot_before_hijack` probe, and
# because the failing run left the regenerated file behind, the next run
# compared against the bad recording and passed. Twice on 2026-08-09/10 the only
# trace was a red `quality-gate` with a clean tree.
#
# So: keep the committed bytes, and put the difference somewhere that outlives
# the run.
#   - Every corpus is copied off before its generator runs, and restored from
#     that copy on exit (including a mid-write abort, hence the trap rather than
#     a restore at the bottom). The tree is left as it was found, so a rerun
#     re-tests the same baseline instead of a rewritten one, and genuine drift
#     stays red until someone re-records it on purpose.
#   - What the generator actually produced is saved under GOLDENS_REGEN_DIR and
#     diffed into the log, so a flake's output can be read after the fact.
#
# Re-recording is NOT done through this script — run the generator directly, as
# the failure message says. That is why an unconditional restore is safe here.
_restore_dir="$(mktemp -d)"
_restore_manifest="$_restore_dir/manifest"
: > "$_restore_manifest"

# Where the regenerated (rejected) content is kept for inspection. Overridable
# so CI can point it at a path it uploads as an artifact.
GOLDENS_REGEN_DIR="${GOLDENS_REGEN_DIR:-}"

# How much of each diff to print inline. The full file is always saved.
GOLDENS_DIFF_LINES="${GOLDENS_DIFF_LINES:-80}"

# A corpus path flattened into a single filename, so copies from different
# testdata directories cannot collide.
_flatten_path() {
  printf '%s' "${1#./}" | tr '/' '_'
}

_restore_corpora() {
  local saved corpus
  while IFS=$'\t' read -r saved corpus; do
    # `cmp` first: copying back an identical file still bumps its mtime, and on
    # a passing run that is every corpus in the repository. Written as an `if`
    # rather than an `&&` chain because this runs from an EXIT trap under
    # `set -e`, where a chain evaluating false is an error.
    if [[ -f "$saved" ]] && ! cmp -s "$saved" "$corpus"; then
      cp "$saved" "$corpus"
    fi
  done < "$_restore_manifest"
  rm -rf "$_restore_dir"
}
trap _restore_corpora EXIT

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

  # Stash the committed bytes before handing the file to the generator; the
  # EXIT trap puts them back whatever happens next.
  saved="$_restore_dir/$(_flatten_path "$corpus")"
  cp "$corpus" "$saved"
  printf '%s\t%s\n' "$saved" "$corpus" >> "$_restore_manifest"

  before="$(shasum -a 256 "$corpus" | cut -d' ' -f1)"
  "${run[@]}" >/dev/null
  after="$(shasum -a 256 "$corpus" | cut -d' ' -f1)"
  checked=$((checked + 1))

  if [[ "$before" != "$after" ]]; then
    echo "stale golden corpus: $corpus"
    echo "  regenerate with: ${run[*]}"

    # Created on first failure only, so a passing run leaves no stray directory.
    if [[ -z "$GOLDENS_REGEN_DIR" ]]; then
      GOLDENS_REGEN_DIR="$(mktemp -d)"
    fi
    mkdir -p "$GOLDENS_REGEN_DIR"
    regenerated="$GOLDENS_REGEN_DIR/$(_flatten_path "$corpus")"
    cp "$corpus" "$regenerated"
    echo "  what the generator produced: $regenerated"

    # -L twice so the header names the two SIDES rather than a temp path.
    # `|| true` twice: diff exits 1 on a difference (which is the expected case
    # here), and head closing the pipe early would trip pipefail.
    echo "  committed (-) vs regenerated (+), first ${GOLDENS_DIFF_LINES} lines:"
    { diff -u -L "committed:$corpus" -L "regenerated:$corpus" "$saved" "$corpus" || true; } |
      head -n "$GOLDENS_DIFF_LINES" | sed 's/^/    /' || true

    stale+=("$corpus")
  fi
done < <(find_generators)

if (( ${#stale[@]} > 0 )); then
  echo
  echo "FAIL: ${#stale[@]} golden corpus file(s) do not match the CPython reference."
  if [[ -n "$GOLDENS_REGEN_DIR" ]]; then
    echo "      Rejected output kept in: $GOLDENS_REGEN_DIR"
    echo "      The working tree is unchanged — the committed corpora were restored."
  fi
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
done < <(find_corpora)

if (( ungenerated > 0 )); then
  echo "NOTE: $ungenerated committed corpus file(s) have no generator and cannot be drift-checked."
fi
