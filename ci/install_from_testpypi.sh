#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install a published package version from TestPyPI, falling back to PyPI for
# transitive deps, retrying to absorb TestPyPI index-propagation lag.
#
# Usage: ci/install_from_testpypi.sh <package> <version>
set -euo pipefail

package="${1:?usage: install_from_testpypi.sh <package> <version>}"
version="${2:?usage: install_from_testpypi.sh <package> <version>}"
PYTHON="${PYTHON:-python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# How long to keep asking TestPyPI for a file it has already accepted.
#
# Publishing returns 200 well before the simple index serves the new version,
# and the gap is not small. The v0.5.2 rehearsal measured it: `provide-uterm`
# uploaded at 00:09:00, was still absent at 00:11:40, and was being served by
# 00:12:54 -- between 2m40s and 3m54s. The budget then was five attempts 30s
# apart, so verification declared a successful publish broken with 2m30s of
# patience against propagation that needed more.
#
# Ten minutes is not a guess at the true worst case; it is chosen to be well
# clear of a number we have only ever sampled from below. This costs nothing on
# the normal path -- the loop exits on the first success, which is usually the
# first or second attempt -- and it is only ever spent on a release job.
ATTEMPTS="${TESTPYPI_INSTALL_ATTEMPTS:-20}"
DELAY_SECONDS="${TESTPYPI_INSTALL_DELAY:-30}"

# Extras come from scripts/package_metadata.py rather than a copy of the table
# here. Verification imports every name in a package's import_names, and some of
# those pull dependencies that only a bare `pip install name` leaves out --
# provide-uterm-platform imports provide.uterm.manager, whose fastapi sits in the
# `manager` extra. Installing without it is what failed the 0.5.1 release with
# "ModuleNotFoundError: No module named 'fastapi'", after the publish itself had
# already succeeded.
spec="$("$PYTHON" "$ROOT/scripts/package_metadata.py" "$package")"
echo "installing ${spec}==${version}"

for i in $(seq 1 "${ATTEMPTS}"); do
  if pip install \
       --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ \
       "${spec}==${version}"; then
    exit 0
  fi
  # No sleep after the last attempt: it delays the failure without buying
  # another look at the index.
  if [ "${i}" -lt "${ATTEMPTS}" ]; then
    echo "Attempt ${i}/${ATTEMPTS} failed — waiting ${DELAY_SECONDS}s for index propagation…"
    sleep "${DELAY_SECONDS}"
  fi
done

echo "Failed to install ${spec}==${version} from TestPyPI after ${ATTEMPTS} attempts" \
     "over ~$(( (ATTEMPTS - 1) * DELAY_SECONDS / 60 )) minutes" >&2
exit 1
