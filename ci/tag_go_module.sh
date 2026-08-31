#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Publish the Go port by tagging it, and warm the module proxy.
#
# Go has no upload step: a module is published by pushing a tag the proxy can
# resolve. For a module in a subdirectory that tag MUST carry the module path as
# a prefix -- packages/provide-uterm-go/vX.Y.Z -- because a bare vX.Y.Z names the
# repository root, where there is no go.mod. So the release tag alone publishes
# nothing, and until this ran the Go port had never been consumable by version at
# all while its siblings shipped to PyPI and NuGet.
#
# Idempotent: a tag that already exists is left alone rather than failing, so a
# re-run of a release does not die on work already done -- the same reason
# skip-existing is on the PyPI steps and --skip-duplicate on the NuGet one.
set -euo pipefail

cd "$(dirname "$0")/.."

module_dir="packages/provide-uterm-go"
module_path="github.com/provide-io/provide-uterm/${module_dir}"
version="$(tr -d '[:space:]' < "${module_dir}/VERSION")"
tag="${module_dir}/v${version}"

summary() { [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$1" >> "${GITHUB_STEP_SUMMARY}"; printf '%s\n' "$1"; }

# A tag that disagrees with the VERSION file means the cut is wrong. Unlike a
# package feed a git tag can be moved, but a module proxy caches what it fetched
# and will keep serving the first content it saw, so this is just as one-way.
if [ -n "${RELEASE_TAG:-}" ] && [ "${RELEASE_TAG#v}" != "${version}" ]; then
  echo "::error::release tag ${RELEASE_TAG} does not match Go VERSION ${version}" >&2
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/${tag}" >/dev/null 2>&1; then
  summary "### Go module: \`${tag}\` already tagged"
else
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git tag -a "${tag}" -m "provide-uterm Go module ${version}"
  git push origin "${tag}"
  summary "### Go module: tagged \`${tag}\`"
fi

# Fetching the .info endpoint is what makes the proxy resolve and cache the
# version; pkg.go.dev then indexes from the proxy on its own schedule. Best
# effort -- the tag is the publish, this only shortens the wait.
if curl -fsS --max-time 60 "https://proxy.golang.org/${module_path}/@v/v${version}.info" >/dev/null 2>&1; then
  summary "Module proxy resolved \`${module_path}@v${version}\`."
else
  summary "Module proxy has not resolved \`v${version}\` yet; it will on first fetch."
fi
