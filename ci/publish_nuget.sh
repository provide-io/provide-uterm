#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Pack the C# port and push it to nuget.org.
#
# Packing always runs, so a release proves the package still builds even when it
# cannot be pushed. Pushing needs NUGET_API_KEY, which only exists once someone
# has configured it; until then this reports the skip into the run summary
# rather than failing the release or, worse, passing quietly. That is the same
# shape as the cosign notice in scripts/release_governance_check.sh -- and the
# lesson of provide-uterm-annotation, which sat three versions behind because
# nothing said out loud that it was not being published.
set -euo pipefail

cd "$(dirname "$0")/.."

project="packages/provide-uterm-csharp/src/Provide.Uterm/Provide.Uterm.csproj"
outdir="${NUGET_OUT_DIR:-nupkg}"
version="$(tr -d '[:space:]' < packages/provide-uterm-csharp/VERSION)"

summary() { [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$1" >> "${GITHUB_STEP_SUMMARY}"; printf '%s\n' "$1"; }

rm -rf "${outdir}"
dotnet pack "${project}" -c Release -o "${outdir}"

# The version in the package must be the version being released. A tag that
# disagrees with packages/provide-uterm-csharp/VERSION means the cut is wrong,
# and pushing it would put a mislabelled package on a feed that never forgets.
if [ -n "${RELEASE_TAG:-}" ] && [ "${RELEASE_TAG#v}" != "${version}" ]; then
  echo "::error::release tag ${RELEASE_TAG} does not match C# VERSION ${version}" >&2
  exit 1
fi

if [ -z "${NUGET_API_KEY:-}" ]; then
  summary "### NuGet: packed ${version}, not pushed"
  summary ""
  summary "\`NUGET_API_KEY\` is not configured, so \`Provide.Uterm.${version}\` was built and"
  summary "verified but not published. Configure the secret to enable pushing."
  exit 0
fi

# --skip-duplicate for the same reason skip-existing is on the PyPI steps: a
# re-run must not die on a version the feed already has.
dotnet nuget push "${outdir}"/*.nupkg \
  --source https://api.nuget.org/v3/index.json \
  --api-key "${NUGET_API_KEY}" \
  --skip-duplicate

summary "### NuGet: pushed \`Provide.Uterm.${version}\` (+ symbols)"
