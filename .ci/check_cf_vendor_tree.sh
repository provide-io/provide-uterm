#!/usr/bin/env bash
# Validates the CF Worker python_modules vendor tree. The Worker needs both
# the external `provide.shell` package AND the internal `provide.uterm.shell`
# submodule vendored. A missing tree indicates `pywrangler sync` wasn't run.
set -euo pipefail

VENDOR=packages/provide-uterm-cloudflare/python_modules
STRICT=${UTERM_VENDOR_GUARD_STRICT:-0}

if [ ! -d "$VENDOR" ] || [ -z "$(find "$VENDOR" -mindepth 2 -name '*.py' -print -quit)" ]; then
    if [ "$STRICT" = "1" ]; then
        echo "ERROR: CF vendor tree is absent or empty ($VENDOR)"
        exit 1
    fi
    echo "WARNING: CF vendor tree absent or empty; skipping non-strict vendor check ($VENDOR)"
    exit 0
fi

for sub in "provide/shell" "provide/uterm/shell"; do
    path="$VENDOR/$sub"
    if [ ! -d "$path" ]; then
        if [ "$STRICT" != "1" ]; then
            echo "WARNING: $sub missing from CF vendor tree; skipping non-strict vendor check ($path)"
            exit 0
        fi
        echo "ERROR: $sub missing from CF vendor tree ($path)"
        exit 1
    fi
    if [ -z "$(find "$path" -name '*.py' -print -quit)" ]; then
        if [ "$STRICT" != "1" ]; then
            echo "WARNING: $sub vendor tree is empty; skipping non-strict vendor check ($path)"
            exit 0
        fi
        echo "ERROR: $sub vendor tree is empty ($path)"
        exit 1
    fi
done
