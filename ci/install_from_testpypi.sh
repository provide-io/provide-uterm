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

# The index normalises names to lowercase with dashes; the FILES it lists use
# the underscored distribution name. Match on the file so a project page that
# exists but has not yet listed this version does not read as success.
file_stem="${package//-/_}-${version}"

echo "waiting for ${package}==${version} on ${TESTPYPI_SIMPLE}"
served=0
for i in $(seq 1 "${WAIT_ATTEMPTS}"); do
  if curl -sSf --max-time 30 "${TESTPYPI_SIMPLE}/${package}/" 2>/dev/null | grep -qF "${file_stem}"; then
    echo "index is serving ${file_stem} (attempt ${i})"
    served=1
    break
  fi
  if [ "${i}" -lt "${WAIT_ATTEMPTS}" ]; then
    echo "not indexed yet (attempt ${i}/${WAIT_ATTEMPTS}) — waiting ${WAIT_SECONDS}s…"
    sleep "${WAIT_SECONDS}"
  fi
done

if [ "${served}" -ne 1 ]; then
  echo "TestPyPI never served ${file_stem} after ~$(( (WAIT_ATTEMPTS - 1) * WAIT_SECONDS / 60 )) minutes" >&2
  exit 1
fi

# Phase 1: fetch OUR distributions from TestPyPI, resolving nothing.
#
# Every package in this release that is already up is pulled, not just the one
# under test: a dependent names its siblings, and if they are absent here pip
# would satisfy `provide-uterm>=0.5.0` from PyPI with the PREVIOUS release and
# verify the new package against an old core. Siblings published later in the
# pipeline simply are not there yet, which is why a miss is tolerated.
wheelhouse="$(mktemp -d)"
trap 'rm -rf "${wheelhouse}"' EXIT

for name in $("$PYTHON" "$ROOT/scripts/package_metadata.py" --names); do
  if pip download --no-deps --no-binary :none: \
       --index-url "${TESTPYPI_SIMPLE}/" \
       --dest "${wheelhouse}" \
       "${name}==${version}" >/dev/null 2>&1; then
    echo "  fetched ${name}==${version} from TestPyPI"
  else
    echo "  ${name}==${version} not on TestPyPI yet — skipping"
  fi
done

# Phase 2: install against PyPI ALONE, with our own files offered locally.
# TestPyPI is not an index here, so no name it hosts can win a resolution.
echo "installing ${spec}==${version}"
pip install \
  --index-url "${PYPI_SIMPLE}/" \
  --find-links "${wheelhouse}" \
  "${spec}==${version}"
