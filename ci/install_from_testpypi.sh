#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install a just-published package version from TestPyPI and prove it imports.
#
# Two things make this harder than it looks, both of which have failed a real
# release:
#
#   1. TestPyPI must not take part in dependency resolution at all. It is not a
#      mirror of PyPI; it is full of whatever anyone uploaded to a name first.
#      `provide-uterm-platform[manager]` needs fastapi>=0.110, and TestPyPI
#      carries a squatted "FASTAPI 1.0" -- a 2.5 kB sdist that cannot be built
#      ("FileNotFoundError: DESCRIPTION.txt"). Version 1.0 outranks the real
#      0.141.1 on PyPI, and pip picks the highest version across ALL configured
#      indexes, so index ORDER does not help: making PyPI primary and TestPyPI
#      the extra still resolves fastapi to the squat. That is dependency
#      confusion, and the only fix is to stop asking TestPyPI about third-party
#      names. So our own distributions are fetched from TestPyPI first with
#      --no-deps, and the real install then runs against PyPI alone with those
#      files offered via --find-links.
#
#   2. Publishing returns 200 long before the index serves the file, and the lag
#      varies by more than an order of magnitude. Measured on v0.5.2: provide-
#      uterm took under four minutes; provide-uterm-cloudflare was still absent
#      after nine and appeared somewhere before twenty-eight. A budget set from
#      the first number fails the second.
#
# Waiting is therefore separated from installing. Polling the simple index
# directly is one cheap request that asks exactly one question -- is the file
# being served yet -- so a slow index reports as waiting, and a genuinely broken
# package reports as a failed install. When those were the same retry loop, a
# ten-minute wait and an unbuildable dependency produced the same message.
#
# Usage: ci/install_from_testpypi.sh <package> <version>
set -euo pipefail

package="${1:?usage: install_from_testpypi.sh <package> <version>}"
version="${2:?usage: install_from_testpypi.sh <package> <version>}"
PYTHON="${PYTHON:-python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TESTPYPI_SIMPLE="${TESTPYPI_SIMPLE_URL:-https://test.pypi.org/simple}"
PYPI_SIMPLE="${PYPI_SIMPLE_URL:-https://pypi.org/simple}"

# Thirty minutes, against a worst case we have only ever sampled from below --
# we know cloudflare took longer than nine minutes and no longer than twenty-
# eight, because those are the two times anybody looked. It costs nothing on the
# normal path: the poll returns as soon as the file appears, usually on the
# first or second request, and it is only ever spent on a release job.
WAIT_ATTEMPTS="${TESTPYPI_WAIT_ATTEMPTS:-60}"
WAIT_SECONDS="${TESTPYPI_WAIT_DELAY:-30}"

# Extras come from scripts/package_metadata.py rather than a copy of the table
# here. Verification imports every name in a package's import_names, and some of
# those pull dependencies that only a bare `pip install name` leaves out --
# provide-uterm-platform imports provide.uterm.manager, whose fastapi sits in the
# `manager` extra. Installing without it is what failed the 0.5.1 release with
# "ModuleNotFoundError: No module named 'fastapi'", after the publish itself had
# already succeeded.
spec="$("$PYTHON" "$ROOT/scripts/package_metadata.py" "$package")"

# Phase 1: fetch OUR distributions from TestPyPI, resolving nothing.
#
# The retry lives here, around the download that will actually be consumed,
# rather than around a separate probe of the index. An earlier version polled
# the simple page with curl and then installed with pip, which is two different
# views of the same index: on v0.5.3 the curl probe reported "index is serving
# provide_uterm-0.5.3" and pip, three seconds later, reported "from versions:
# 0.5.1, 0.5.2". Two curls a minute apart disagreed with each other as well, so
# the edges were simply still converging. A gate that a later step can fail is
# not a gate. Whoever waits must be whoever consumes.
#
# Every package in this release that is already up is pulled, not just the one
# under test: a dependent names its siblings, and if they are absent here pip
# would satisfy `provide-uterm>=0.5.0` from PyPI with the PREVIOUS release and
# verify the new package against an old core. Siblings are fetched only after
# the package under test has appeared, by which point the index has demonstrably
# converged; one that is still missing then belongs to a stage of the pipeline
# that has not run yet, which is the one case worth tolerating.
wheelhouse="$(mktemp -d)"
trap 'rm -rf "${wheelhouse}"' EXIT

# --no-cache-dir is load-bearing, not hygiene. pip caches the index response,
# so once it has seen a page without this version it will keep answering from
# that copy: locally, pip reported "from versions: 0.5.0, 0.5.1, 0.5.2" forty
# minutes after 0.5.3 was published and served by both the HTML and the PEP 691
# JSON views, and the same command with --no-cache-dir downloaded it at once.
# A retry loop around a cached negative answer is decorative -- every one of the
# sixty attempts would re-read the same stale page and the job would spend
# thirty minutes to fail exactly as fast as it would have failed immediately.
fetch() {  # fetch <name>; no dependency resolution, TestPyPI only, uncached
  pip download --no-deps --no-cache-dir \
    --index-url "${TESTPYPI_SIMPLE}/" \
    --dest "${wheelhouse}" \
    "${1}==${version}"
}

echo "waiting for ${package}==${version} on ${TESTPYPI_SIMPLE}"
fetched=0
for i in $(seq 1 "${WAIT_ATTEMPTS}"); do
  if fetch "${package}" > "${wheelhouse}/.fetch.log" 2>&1; then
    echo "fetched ${package}==${version} (attempt ${i})"
    fetched=1
    break
  fi
  if [ "${i}" -lt "${WAIT_ATTEMPTS}" ]; then
    echo "not downloadable yet (attempt ${i}/${WAIT_ATTEMPTS}) — waiting ${WAIT_SECONDS}s…"
    sleep "${WAIT_SECONDS}"
  fi
done

if [ "${fetched}" -ne 1 ]; then
  # The last error, not a summary of it. The previous version discarded pip's
  # output and reported "not on TestPyPI yet" for what was really "pip cannot
  # see it yet", which sent the reader to the wrong index.
  echo "could not download ${package}==${version} from TestPyPI after" \
       "~$(( (WAIT_ATTEMPTS - 1) * WAIT_SECONDS / 60 )) minutes; last pip output:" >&2
  cat "${wheelhouse}/.fetch.log" >&2
  exit 1
fi

for name in $("$PYTHON" "$ROOT/scripts/package_metadata.py" --names); do
  [ "${name}" = "${package}" ] && continue
  if fetch "${name}" >/dev/null 2>&1; then
    echo "  fetched sibling ${name}==${version}"
  else
    echo "  sibling ${name}==${version} not published yet — skipping"
  fi
done

# Phase 2: install against PyPI ALONE, with our own files offered locally.
# TestPyPI is not an index here, so no name it hosts can win a resolution.
echo "installing ${spec}==${version}"
pip install \
  --index-url "${PYPI_SIMPLE}/" \
  --find-links "${wheelhouse}" \
  "${spec}==${version}"
