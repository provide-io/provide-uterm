#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the Go language-server Docker image for vulnerability scanning.
# Usage: ci/build_go_image.sh [image-tag]
set -euo pipefail

image_tag="${1:-provide-uterm-server-go:scan}"

docker build \
  -f docker/Dockerfile.go \
  -t "${image_tag}" \
  .
