#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# One timestamped snapshot of the mutable state that is easy to report wrongly.
#
# Why this exists: a state claim is only true as of the moment it was read, and
# the gap between reading and saying is where the wrong answers live. Over one
# session: a Release run was reported as "never triggered" when it fired fifteen
# minutes later; two browsers were reported as sitting at a login page four tool
# calls after they had been logged into; a scheduled CI job was reported green
# because the failing one sat at position 31 of 49 and the jobs API stops at 30.
# None of those were hard to check. They were checked, and then the answer was
# repeated after it had gone stale.
#
# So this makes re-reading one cheap call instead of six, and stamps its own
# output so the freshness travels with the answer.
#
# Read-only: it queries, it never changes anything.
#
# Usage: scripts/state.sh [section ...]
#   sections: git ci release pypi   (default: all)
set -uo pipefail

REPO="${REPO:-provide-io/provide-uterm}"
# The release pipeline's five packages, plus annotation, which is published but
# is NOT in release.yml's build matrix.
PACKAGES="${PACKAGES:-provide-uterm provide-uterm-server provide-uterm-client provide-uterm-platform provide-uterm-cloudflare provide-uterm-annotation}"

want() {
  [ "$#" -eq 0 ] && return 0
  for s in "$@"; do [ "$s" = "$SECTION" ] && return 0; done
  return 1
}

SECTIONS=("$@")
echo "=== state as of $(date -u +%Y-%m-%dT%H:%M:%SZ) · repo=${REPO} ==="

SECTION=git
if want "${SECTIONS[@]+"${SECTIONS[@]}"}"; then
  echo
  echo "## git"
  printf '  HEAD      %s\n' "$(git log -1 --format='%h %s' 2>/dev/null || echo '(not a repo)')"
  printf '  branch    %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  printf '  tracking  %s\n' "$(git status -sb 2>/dev/null | head -1)"
  dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  printf '  dirty     %s file(s)\n' "$dirty"
  printf '  vs tag    %s commit(s) since %s\n' \
    "$(git rev-list --count "$(git describe --tags --abbrev=0 2>/dev/null)"..HEAD 2>/dev/null || echo '?')" \
    "$(git describe --tags --abbrev=0 2>/dev/null || echo '(none)')"
fi

# Latest run for one workflow and one event, with its failing jobs. Jobs are
# fetched with --paginate and per_page=100 on purpose: the default page is 30,
# and a run with 49 jobs silently hides everything past the thirtieth.
run_line() {
  local wf="$1" event="$2" label="$3" runs id concl created url
  runs=$(gh api "repos/${REPO}/actions/workflows/${wf}/runs?branch=main&event=${event}&per_page=1" 2>/dev/null) || {
    printf '  %-22s (could not read)\n' "$label"; return 0; }
  id=$(echo "$runs" | jq -r '.workflow_runs[0].id // empty')
  [ -z "$id" ] && { printf '  %-22s (no run)\n' "$label"; return 0; }
  concl=$(echo "$runs" | jq -r '.workflow_runs[0].conclusion // .workflow_runs[0].status')
  created=$(echo "$runs" | jq -r '.workflow_runs[0].created_at' | cut -c1-16)
  url=$(echo "$runs" | jq -r '.workflow_runs[0].html_url')
  printf '  %-22s %-11s %s  %s\n' "$label" "$concl" "$created" "$url"
  [ "$concl" = "success" ] && return 0
  gh api --paginate "repos/${REPO}/actions/runs/${id}/jobs?per_page=100" 2>/dev/null \
    | jq -r '.jobs[] | select(.conclusion=="failure") | "      FAIL  \(.name)"' | sort -u
}

SECTION=ci
if want "${SECTIONS[@]+"${SECTIONS[@]}"}"; then
  echo
  echo "## ci (main)"
  run_line ci.yml push "CI · push"
  # The scheduled run executes jobs no push does, so a green push says nothing
  # about it.
  run_line ci.yml schedule "CI · schedule"
  run_line mutation-full.yml schedule "mutation-full"
  run_line hostile-client.yml push "hostile-client"
fi

SECTION=release
if want "${SECTIONS[@]+"${SECTIONS[@]}"}"; then
  echo
  echo "## release"
  rid=$(gh api "repos/${REPO}/actions/workflows/release.yml/runs?per_page=1" 2>/dev/null \
    | jq -r '.workflow_runs[0].id // empty')
  if [ -z "$rid" ]; then
    echo "  (no release run)"
  else
    gh api "repos/${REPO}/actions/runs/${rid}" 2>/dev/null | jq -r \
      '"  run \(.id)  \(.conclusion // .status)  ref=\(.head_branch)  \(.created_at[0:16])\n  \(.html_url)"'
    gh api --paginate "repos/${REPO}/actions/runs/${rid}/jobs?per_page=100" 2>/dev/null \
      | jq -r '.jobs[] | select(.name|test("PyPI")) | "      \(.conclusion // .status)\t\(.name)"'
  fi
  echo "  releases:"
  gh api "repos/${REPO}/releases" 2>/dev/null \
    | jq -r '.[] | "      \(.tag_name)  draft=\(.draft)  assets=\(.assets|length)"' | head -5
fi

SECTION=pypi
if want "${SECTIONS[@]+"${SECTIONS[@]}"}"; then
  echo
  echo "## pypi (simple index — the JSON API serves stale data for minutes after an upload)"
  for idx_host in pypi.org test.pypi.org; do
    printf '  %s\n' "$idx_host"
    for pkg in $PACKAGES; do
      body=$(curl -sf -H 'Cache-Control: no-cache' "https://${idx_host}/simple/${pkg}/" 2>/dev/null) || {
        printf '    %-28s (absent)\n' "$pkg"; continue; }
      latest=$(echo "$body" \
        | grep -oE "$(echo "$pkg" | tr '-' '_')-[0-9]+\.[0-9]+\.[0-9]+" \
        | sed "s/^$(echo "$pkg" | tr '-' '_')-//" | sort -uV | tail -1)
      printf '    %-28s %s\n' "$pkg" "${latest:-(no versions)}"
    done
  done
fi

echo
echo "=== end state · re-run before repeating any of this ==="
