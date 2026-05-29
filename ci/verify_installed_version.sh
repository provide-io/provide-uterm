#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Confirm the installed distribution version matches the expected version.
#
# Usage: ci/verify_installed_version.sh <package> <expected-version>
set -euo pipefail

package="${1:?usage: verify_installed_version.sh <package> <expected-version>}"
expected="${2:?usage: verify_installed_version.sh <package> <expected-version>}"

installed="$(pip show "${package}" | grep '^Version:' | awk '{print $2}')"
echo "Installed: ${installed}  Expected: ${expected}"
[ "${installed}" = "${expected}" ]
