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

for i in 1 2 3 4 5; do
  if pip install \
       --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ \
       "${package}==${version}"; then
    exit 0
  fi
  echo "Attempt ${i} failed — waiting 30s for index propagation…"
  sleep 30
done

echo "Failed to install ${package}==${version} from TestPyPI after 5 attempts" >&2
exit 1
