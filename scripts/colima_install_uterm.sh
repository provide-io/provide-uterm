#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# colima_install_uterm.sh
#
# Build and install pam_uterm.so + libuterm_capture.so into the running Colima VM,
# then wire up /etc/pam.d/sshd so PAM events are forwarded to PamNotifyListener.
#
# Usage (from repo root, with Colima running):
#   bash scripts/colima_install_uterm.sh
#
# What it does:
#   1. Installs build deps (gcc, make, libpam0g-dev) inside Colima
#   2. Builds libuterm_capture.so from native/capture/
#   3. Builds pam_uterm.so from native/pam_uterm/
#   4. Installs both .so files to /usr/local/lib/ and /lib/security/
#   5. Adds a session line to /etc/pam.d/sshd (idempotent)
#   6. Prints verification instructions
#
# After running, start the provide-terminal server on the host and SSH in
# to see PamNotifyListener receive the open/close events.

set -euo pipefail

PTY_PKG="/Users/tim/code/gh/provide-io/provide-terminal/packages/provide-terminal-platform"
CAPTURE_SRC="$PTY_PKG/native/capture"
PAM_SRC="$PTY_PKG/native/pam_uterm"
# Socket inside the Colima VM (/run is root-owned; /tmp is world-writable)
NOTIFY_SOCKET="/tmp/uterm-notify.sock"
CAP_LIB="/usr/local/lib/libuterm_capture.so"
# Use arch-specific path as primary; /lib/security is a fallback on some distros
PAM_MODULE_ARCH="/lib/aarch64-linux-gnu/security/pam_uterm.so"
PAM_MODULE="/lib/security/pam_uterm.so"
PAM_LINE="session  optional  pam_uterm.so socket=${NOTIFY_SOCKET} mode=capture lib=${CAP_LIB} cap_dir=/tmp"

_run() {
    colima ssh -- sh -c "$1"
}

echo "==> Checking Colima is running..."
if ! colima list 2>/dev/null | grep -q "Running"; then
    echo "ERROR: Colima is not running. Start it with: colima start"
    exit 1
fi

echo "==> Installing build dependencies..."
_run "DEBIAN_FRONTEND=noninteractive sudo apt-get update -qq && sudo apt-get install -y -qq gcc make libpam0g-dev python3-pip"

echo "==> Building libuterm_capture.so (Linux aarch64)..."
_run "make -C $CAPTURE_SRC clean && make -C $CAPTURE_SRC"

echo "==> Installing libuterm_capture.so to ${CAP_LIB}..."
_run "sudo install -m 0755 $CAPTURE_SRC/libuterm_capture.so $CAP_LIB && sudo ldconfig"

echo "==> Building pam_uterm.so (Linux aarch64)..."
_run "make -C $PAM_SRC clean && make -C $PAM_SRC"

echo "==> Installing pam_uterm.so..."
_run "sudo mkdir -p /lib/security && sudo install -m 0755 $PAM_SRC/pam_uterm.so $PAM_MODULE"
# Also install to arch-specific directory (Ubuntu aarch64 PAM loads from here)
_run "PAM_ARCH_DIR=\$(ls -d /lib/aarch64-linux-gnu/security /lib/x86_64-linux-gnu/security 2>/dev/null | head -1); if [ -n \"\$PAM_ARCH_DIR\" ]; then sudo install -m 0755 $PAM_SRC/pam_uterm.so \$PAM_ARCH_DIR/pam_uterm.so && echo \"installed to \$PAM_ARCH_DIR\"; fi"

echo "==> Verifying .so files are valid ELF..."
_run "readelf -h $CAP_LIB | grep 'Type:' && readelf -h $PAM_MODULE | grep 'Type:'"

UTERM_PKG="/Users/tim/code/gh/provide-io/provide-terminal/packages/provide-terminal"

echo "==> Installing provide-terminal-platform Python package into Colima system Python..."
_run "python3 -m pip install -q --break-system-packages -e $PTY_PKG"

echo "==> Installing provide-terminal (server + pam_integration) into Colima system Python..."
_run "python3 -m pip install -q --break-system-packages pydantic httpx && python3 -m pip install -q --break-system-packages -e $UTERM_PKG"

echo "==> Wiring up /etc/pam.d/sshd (replaces any existing pam_uterm line)..."
_run "sudo sed -i '/pam_uterm/d' /etc/pam.d/sshd && printf '\n# provide-terminal session capture\n${PAM_LINE}\n' | sudo tee -a /etc/pam.d/sshd > /dev/null"

echo "==> Current pam_uterm lines in /etc/pam.d/sshd:"
_run "grep pam_uterm /etc/pam.d/sshd || echo '  (none found — check above step)'"

echo ""
echo "==> Done."
echo ""
echo "Run the smoke test to verify end-to-end:"
echo "  uv run python scripts/colima_smoke_test.py"
