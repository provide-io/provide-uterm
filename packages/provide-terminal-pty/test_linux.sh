#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Runs inside the Dockerfile.test container (root, Debian-based).
set -e

cd /pkg
pip install -q -e .

echo "=== Building libuterm_capture (Linux .so) ==="
make -C native/capture/ clean && make -C native/capture/
mkdir -p src/provide/terminal/pty/_native
cp native/capture/libuterm_capture.so src/provide/terminal/pty/_native/
ls src/provide/terminal/pty/_native/

echo "=== Building pam_uterm ==="
make -C native/pam_uterm/ clean && make -C native/pam_uterm/
ls native/pam_uterm/*.so

echo "=== Verifying pam_uterm.so exports ==="
nm -D native/pam_uterm/pam_uterm.so | grep pam_sm

echo "=== Setting up PAM service for unit tests ==="
printf 'auth     required pam_unix.so\naccount  required pam_unix.so\nsession  required pam_unix.so\n' \
    > /etc/pam.d/provide-terminal

echo "=== Creating test user ==="
useradd -m testuser 2>/dev/null || true
echo 'testuser:testpass123' | chpasswd
id testuser

echo ""
echo "=== Running unit tests (PAM + root markers active) ==="
pytest tests/ -v --no-header -p no:randomly \
    --ignore=tests/e2e \
    --timeout=10

echo ""
echo "=== Running e2e tests (pam_uterm.so → PamNotifyListener chain) ==="
pytest tests/e2e/ -v --no-header -p no:randomly --timeout=15
