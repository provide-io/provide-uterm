#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build language-server Docker images and assert /healthz returns healthy JSON.
# Invoked from CI (docker-smoke job) and locally for proof.
#
# Usage (from repo root):
#   bash ci/docker_language_smoke.sh
#   bash ci/docker_language_smoke.sh python go     # subset
#   EVIDENCE_DIR=/path bash ci/docker_language_smoke.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EVIDENCE_DIR="${EVIDENCE_DIR:-${SCRATCH:-}}"
if [[ -z "${EVIDENCE_DIR}" ]]; then
  if [[ -n "${RUNNER_TEMP:-}" ]]; then
    EVIDENCE_DIR="${RUNNER_TEMP}/uterm-docker-smoke"
  else
    EVIDENCE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/uterm-docker-smoke.XXXXXX")"
  fi
fi
mkdir -p "${EVIDENCE_DIR}"

# Languages to smoke (default: all three).
if [[ $# -eq 0 ]]; then
  LANGS=(python go csharp)
else
  LANGS=("$@")
fi

CFG_HOST="${ROOT}/docker/etc-uterm"
if [[ ! -f "${CFG_HOST}/server.toml" ]]; then
  echo "error: missing ${CFG_HOST}/server.toml (directory mount source)" >&2
  exit 1
fi

# Prefer $HOME for Docker Desktop share roots (not /var/folders).
RUN_CFG="${HOME}/.cache/uterm-docker-smoke-etc-$$"
mkdir -p "${RUN_CFG}"
cp "${CFG_HOST}/server.toml" "${RUN_CFG}/server.toml"
cleanup() {
  docker rm -f uterm-smoke-py uterm-smoke-go uterm-smoke-cs 2>/dev/null || true
  rm -rf "${RUN_CFG}"
}
trap cleanup EXIT

build_one() {
  local lang="$1" df tag
  case "${lang}" in
    python) df=docker/Dockerfile.server; tag=provide-uterm-server:local ;;
    go)     df=docker/Dockerfile.go;     tag=provide-uterm-server-go:local ;;
    csharp) df=docker/Dockerfile.csharp; tag=provide-uterm-server-csharp:local ;;
    *) echo "unknown language: ${lang}" >&2; exit 2 ;;
  esac
  echo "==> docker build ${lang} (${df})"
  docker build -f "${df}" -t "${tag}" . \
    2>&1 | tee "${EVIDENCE_DIR}/docker-build-${lang}.log" | tail -20
}

run_and_curl() {
  local lang="$1" tag name port
  case "${lang}" in
    python) tag=provide-uterm-server:local; name=uterm-smoke-py; port=27780 ;;
    go)     tag=provide-uterm-server-go:local; name=uterm-smoke-go; port=27781 ;;
    csharp) tag=provide-uterm-server-csharp:local; name=uterm-smoke-cs; port=27782 ;;
  esac
  docker rm -f "${name}" 2>/dev/null || true
  docker run -d --name "${name}" -p "${port}:27780" \
    -v "${RUN_CFG}:/etc/uterm:ro" \
    "${tag}"
  local i body code=000
  for i in $(seq 1 40); do
    code=$(curl -sS -o "${EVIDENCE_DIR}/health-${lang}.json" -w '%{http_code}' \
      --max-time 2 "http://127.0.0.1:${port}/healthz" 2>/dev/null || echo 000)
    if [[ "${code}" == "200" ]]; then
      break
    fi
    sleep 1
  done
  body="$(cat "${EVIDENCE_DIR}/health-${lang}.json" 2>/dev/null || true)"
  echo "${lang}: http=${code} body=${body}"
  if [[ "${code}" != "200" ]]; then
    echo "=== logs ${name} ===" >&2
    docker logs "${name}" 2>&1 | tail -40 >&2 || true
    echo "FAIL: ${lang} /healthz not healthy" >&2
    exit 1
  fi
  if ! grep -q '"status"' "${EVIDENCE_DIR}/health-${lang}.json"; then
    echo "FAIL: ${lang} health body missing status" >&2
    exit 1
  fi
}

echo "evidence → ${EVIDENCE_DIR}"
for lang in "${LANGS[@]}"; do
  build_one "${lang}"
done
for lang in "${LANGS[@]}"; do
  run_and_curl "${lang}"
done

docker compose -f docker/docker-compose.yml config --services \
  | tee "${EVIDENCE_DIR}/compose-services.txt"

# Assert compose uses directory mounts (not file→file server.toml only).
if docker compose -f docker/docker-compose.yml config 2>/dev/null \
  | grep -E 'source:.*dev-smoke\.toml' >/dev/null; then
  echo "WARN: compose still references file-bind dev-smoke.toml" >&2
fi
if ! docker compose -f docker/docker-compose.yml config 2>/dev/null \
  | grep -E 'etc-uterm|/etc/uterm' >/dev/null; then
  echo "FAIL: compose config missing /etc/uterm directory mount" >&2
  exit 1
fi

echo "OK docker language smoke: ${LANGS[*]}"
echo "logs and health JSON under ${EVIDENCE_DIR}"
