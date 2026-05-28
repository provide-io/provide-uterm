#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the reference server Docker image for vulnerability scanning. The image
# is not pushed anywhere — it is built locally so Trivy can scan it.
#
# Usage: ci/build_server_image.sh [image-tag]
set -euo pipefail

image_tag="${1:-provide-uterm-server:scan}"

docker build \
  -f docker/Dockerfile.server \
  -t "${image_tag}" \
  .
