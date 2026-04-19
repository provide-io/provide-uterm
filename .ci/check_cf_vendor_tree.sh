#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Validates the CF Worker python_modules vendor tree. The Worker needs both
# the external `provide.shell` package AND the internal `provide.terminal.shell`
# submodule vendored. A missing tree indicates `pywrangler sync` wasn't run.
set -euo pipefail

VENDOR=packages/provide-terminal-cloudflare/python_modules

for sub in "provide/shell" "provide/terminal/shell"; do
    path="$VENDOR/$sub"
    if [ ! -d "$path" ]; then
        echo "ERROR: $sub missing from CF vendor tree ($path)"
        exit 1
    fi
    if [ -z "$(find "$path" -name '*.py' -print -quit)" ]; then
        echo "ERROR: $sub vendor tree is empty ($path)"
        exit 1
    fi
done
