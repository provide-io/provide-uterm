#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Tell site-uterm-io that this repo changed something the site serves.
#
# Why this exists: uterm.io renders demo artifacts that live HERE — the site's
# scripts/sync_uterm_demos.py reads demo/site-manifest.json out of a sibling
# provide-uterm checkout at build time. The site rebuilds on its own pushes, so
# a change on THIS side used to reach production only when somebody happened to
# push the site for an unrelated reason. On 2026-09-03 the deployed site was
# serving a build from 2026-05-17.
#
# What this does NOT solve: the site's prose. Feature pages are hand-written, so
# no dispatch can notice that a page describes seven packages when there are
# eleven. This keeps the SYNCED artifacts fresh; the words are still on us.
#
# Deliberately advisory when unconfigured, loud when broken:
#   - no token in the environment -> job summary note, exit 0. Firing this at a
#     repo we cannot authenticate to is a setup gap, not a broken build, and a
#     red main teaches people to ignore red main.
#   - token present but the dispatch is refused -> exit non-zero. That is a real
#     failure: the wiring exists and stopped working, which is worth a red job.
#
# Requires a token with contents:write on the SITE repo; the per-run GITHUB_TOKEN
# is scoped to this repository and cannot dispatch across repos.
#
# Inputs from the workflow environment: SITE_DISPATCH_TOKEN, GITHUB_SHA,
# GITHUB_STEP_SUMMARY.
set -uo pipefail

# Dispatch target. Kept here rather than inline at the call site so the repo and
# event name are declared in one place.
SITE_REPO="${SITE_REPO:-provide-io/site-uterm-io}"
EVENT_TYPE="${EVENT_TYPE:-uterm-updated}"

note() {
  echo "$1"
  [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && echo "$1" >>"${GITHUB_STEP_SUMMARY}"
  return 0
}

if [[ -z "${SITE_DISPATCH_TOKEN:-}" ]]; then
  note "site deploy not notified: no SITE_DISPATCH_TOKEN secret is set."
  note ""
  note "To wire it up, add a token with \`contents: write\` on ${SITE_REPO} as the"
  note "repository secret \`SITE_DISPATCH_TOKEN\`. Until then uterm.io rebuilds only"
  note "when the site repo itself is pushed, so demo changes here will not ship."
  exit 0
fi

# The site checks out provide-uterm at this ref, so pin the exact commit that
# produced the change rather than letting it resolve main a second time.
payload=$(jq -nc --arg t "${EVENT_TYPE}" --arg ref "${GITHUB_SHA}" \
  '{event_type: $t, client_payload: {uterm_ref: $ref}}')

if ! GH_TOKEN="${SITE_DISPATCH_TOKEN}" gh api \
  --method POST "repos/${SITE_REPO}/dispatches" \
  --input - <<<"${payload}"; then
  echo "::error::repository_dispatch to ${SITE_REPO} was refused."
  echo "::error::SITE_DISPATCH_TOKEN is set, so this is a live failure, not a setup gap:"
  echo "::error::check the token has contents:write on ${SITE_REPO} and has not expired."
  exit 1
fi

note "Dispatched \`${EVENT_TYPE}\` to ${SITE_REPO} at \`${GITHUB_SHA:0:7}\`."
