#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Level B browser-resume e2e against a local Workers runtime.
#
# IMPORTANT: use ``npx wrangler dev`` after ``.ci/vendor_cf_worker.sh``.
# Do **not** use ``pywrangler dev`` here — it re-syncs ``python_modules/`` into
# a layout Pyodide cannot import (missing provide.telemetry / tunnel / etc.).
#
# Usage (repo root):
#   bash scripts/run_cf_resume_e2e_local.sh
#
# Optional:
#   PORT=8989 JWKS_PORT=8990 bash scripts/run_cf_resume_e2e_local.sh
#
# Production (deployed worker) still needs a real CF Access / principal JWT:
#   REAL_CF=1 REAL_CF_URL=https://provide-uterm-cloudflare.neurotic.workers.dev \
#     CF_E2E_JWT=<access-jwt> CF_ACCESS_CLIENT_ID=… CF_ACCESS_CLIENT_SECRET=… \
#     uv run pytest -m real_cf packages/provide-uterm-cloudflare/tests/test_e2e_ws.py \
#     -k 'resume or hello_includes_resume' --no-cov

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8989}"
JWKS_PORT="${JWKS_PORT:-8990}"
CF_PKG="$ROOT/packages/provide-uterm-cloudflare"
STATE_DIR="${TMPDIR:-/tmp}/uterm-cf-resume-e2e-$$"
mkdir -p "$STATE_DIR"
cleanup() {
  if [[ -f "$STATE_DIR/wrangler.pid" ]]; then
    kill "$(cat "$STATE_DIR/wrangler.pid")" 2>/dev/null || true
  fi
  if [[ -f "$STATE_DIR/jwks.pid" ]]; then
    kill "$(cat "$STATE_DIR/jwks.pid")" 2>/dev/null || true
  fi
  rm -rf "$STATE_DIR"
}
trap cleanup EXIT

echo "=== vendor CF python_modules (flat tree) ==="
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true
bash "$ROOT/.ci/vendor_cf_worker.sh"

echo "=== mint RS256 JWT + serve JWKS on :${JWKS_PORT} ==="
uv run python - "$STATE_DIR" "$JWKS_PORT" <<'PY' &
import json
import pathlib
import sys
import http.server

sys.path.insert(0, "packages/provide-uterm-cloudflare/tests")
from cf_jwt_harness import AUDIENCE, ISSUER, build_keypair, mint

state = pathlib.Path(sys.argv[1])
port = int(sys.argv[2])
priv, jwks = build_keypair()
token = mint(priv)
(state / "jwt.env").write_text(
    f"CF_E2E_JWT={token}\nJWT_ISSUER={ISSUER}\nJWT_AUDIENCE={AUDIENCE}\n",
    encoding="utf-8",
)
body = json.dumps(jwks).encode()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: object) -> None:
        return


http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
echo $! >"$STATE_DIR/jwks.pid"

for _ in $(seq 1 50); do
  if [[ -f "$STATE_DIR/jwt.env" ]] && curl -sf "http://127.0.0.1:${JWKS_PORT}/jwks" >/dev/null; then
    break
  fi
  sleep 0.1
done
if [[ ! -f "$STATE_DIR/jwt.env" ]]; then
  echo "ERROR: JWT harness failed to start" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$STATE_DIR/jwt.env"
export CF_E2E_JWT

# Local secrets (gitignored)
BEARER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
cat >"$CF_PKG/.dev.vars" <<EOF
AUTH_MODE=jwt
WORKER_BEARER_TOKEN=${BEARER}
RESUME_ENABLED=1
RESUME_TTL_S=300
EOF

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "ERROR: port $PORT already in use — stop the other worker first" >&2
  exit 1
fi

echo "=== wrangler dev on :${PORT} (JWT harness JWKS) ==="
(
  cd "$CF_PKG"
  npx wrangler dev --port "$PORT" --ip 127.0.0.1 \
    --var AUTH_MODE:jwt \
    --var "JWT_JWKS_URL:http://127.0.0.1:${JWKS_PORT}/jwks" \
    --var JWT_ISSUER:test-iss \
    --var JWT_AUDIENCE:test-aud \
    --var JWT_ALGORITHMS:RS256 \
    --var JWT_DEFAULT_ROLE:admin \
    --var RESUME_ENABLED:1 \
    --var RESUME_TTL_S:300 \
    --var ENVIRONMENT:development
) >"$STATE_DIR/wrangler.log" 2>&1 &
echo $! >"$STATE_DIR/wrangler.pid"

echo "waiting for health..."
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null; then
    echo "ready after ${i}s"
    break
  fi
  sleep 2
  if [[ "$i" -eq 60 ]]; then
    echo "wrangler failed to become healthy; last log:" >&2
    tail -40 "$STATE_DIR/wrangler.log" >&2
    exit 1
  fi
done

export REAL_CF=1
export REAL_CF_URL="http://127.0.0.1:${PORT}"
echo "=== resume e2e (REAL_CF_URL=${REAL_CF_URL}) ==="
uv run pytest -q packages/provide-uterm-cloudflare/tests/test_e2e_ws.py \
  -k "resume or hello_includes_resume" --no-cov -vv

echo ""
echo "Level B local resume e2e: PASS"
