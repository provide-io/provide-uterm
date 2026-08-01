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

# Host ports the smoke containers are published on. Each container always
# listens on 27780 inside; only the host side varies. Overridable because a
# developer machine may already be using these — see the preflight in
# `run_and_curl`, which refuses to curl a port it did not start.
SMOKE_PORT_PYTHON="${SMOKE_PORT_PYTHON:-27780}"
SMOKE_PORT_GO="${SMOKE_PORT_GO:-27781}"
SMOKE_PORT_CSHARP="${SMOKE_PORT_CSHARP:-27782}"
CONTAINER_PORT=27780

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
    python) tag=provide-uterm-server:local; name=uterm-smoke-py; port="${SMOKE_PORT_PYTHON}" ;;
    go)     tag=provide-uterm-server-go:local; name=uterm-smoke-go; port="${SMOKE_PORT_GO}" ;;
    csharp) tag=provide-uterm-server-csharp:local; name=uterm-smoke-cs; port="${SMOKE_PORT_CSHARP}" ;;
  esac
  docker rm -f "${name}" 2>/dev/null || true
  # Refuse to run against a port something else already holds. Without this the
  # curl below happily reaches the foreign service and reports its answer as the
  # image's — a false pass, or a false failure, either way not this image.
  if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FAIL: host port ${port} is already in use, so the ${lang} smoke would test whatever holds it." >&2
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >&2 || true
    echo "Free it, or re-run with SMOKE_PORT_$(echo "${lang}" | tr '[:lower:]' '[:upper:]')=<free port>." >&2
    exit 1
  fi
  docker run -d --name "${name}" -p "${port}:${CONTAINER_PORT}" \
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

# Assert compose uses directory mounts (not fragile file→file server.toml binds).
compose_cfg="$(docker compose -f docker/docker-compose.yml config 2>/dev/null || true)"
if echo "${compose_cfg}" | grep -E 'dev-smoke\.toml:.*/etc/uterm/server\.toml' >/dev/null; then
  echo "FAIL: compose still file-binds dev-smoke.toml onto /etc/uterm/server.toml" >&2
  exit 1
fi
if ! echo "${compose_cfg}" | grep -E 'etc-uterm|target: /etc/uterm' >/dev/null; then
  echo "FAIL: compose config missing /etc/uterm directory mount" >&2
  exit 1
fi

echo "OK docker language smoke: ${LANGS[*]}"
echo "logs and health JSON under ${EVIDENCE_DIR}"
