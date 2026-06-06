#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Self-contained JWT auth harness for the CF Worker e2e tests.

The worker is jwt-only (the ``AUTH_MODE=dev`` bypass was removed), so the
authenticated Durable-Object routes (webhooks, sessions) require a JWT that the
worker's ``decode_jwt`` accepts. In the Pyodide runtime ``decode_jwt`` verifies
RS256 via Web Crypto and fetches the signing key from ``JWT_JWKS_URL`` — so this
harness mints an RS256 keypair, serves a matching JWKS over localhost (workerd
can fetch 127.0.0.1), and issues a short-lived token. Point a local
``wrangler dev`` at the served JWKS and pass the token as ``CF_E2E_JWT`` to drive
the e2e tests with no real Cloudflare Access dependency.

Run it standalone — it prints the token + the ``wrangler dev`` overrides, then
serves the JWKS (blocking):

    uv run python packages/provide-uterm-cloudflare/tests/cf_jwt_harness.py

Then, in another shell (see .provide/HANDOFF.md for the full runbook):

    cd packages/provide-uterm-cloudflare
    npx wrangler dev --port 8989 --ip 127.0.0.1 \\
      --var AUTH_MODE:jwt --var JWT_JWKS_URL:http://127.0.0.1:8990/jwks \\
      --var JWT_ISSUER:test-iss --var JWT_AUDIENCE:test-aud \\
      --var JWT_ALGORITHMS:RS256 --var WORKER_BEARER_TOKEN:<32+ random> \\
      --var WEBHOOK_SECRET_KEY:<base64 AES-256>
    REAL_CF=1 REAL_CF_URL=http://127.0.0.1:8989 CF_E2E_JWT=<token> \\
      uv run pytest packages/provide-uterm-cloudflare/tests/test_e2e_sse_webhooks.py \\
      -k register_with_secret
"""

from __future__ import annotations

import http.server
import json
import time

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "cf-e2e-test-key-1"
JWKS_PORT = 8990
ISSUER = "test-iss"
AUDIENCE = "test-aud"
TOKEN_TTL_S = 3600


def build_keypair() -> tuple[bytes, dict]:
    """Return (PKCS8 private-key PEM, JWKS dict) for a fresh RS256 keypair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return priv_pem, {"keys": [jwk]}


def mint(priv_pem: bytes, *, subject: str = "cf-e2e-test-user", roles: tuple[str, ...] = ("admin",)) -> str:
    """Mint a short-lived RS256 JWT the worker's decode_jwt accepts.

    The principal is granted ``admin`` by default via the ``roles`` claim — the
    CF e2e suite drives admin operations (hijack leases, restarts) that the
    removed ``AUTH_MODE=dev`` mode used to auto-admit.
    """
    now = int(time.time())
    return pyjwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "iat": now, "exp": now + TOKEN_TTL_S, "roles": list(roles)},
        priv_pem,
        algorithm="RS256",
        headers={"kid": KID},
    )


def _serve_jwks(jwks: dict, port: int) -> None:  # pragma: no cover - manual harness entrypoint
    body = json.dumps(jwks).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a: object) -> None:
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover - manual harness entrypoint
    priv, jwks_doc = build_keypair()
    token = mint(priv)
    print(f"CF_E2E_JWT={token}\n")
    print(
        "wrangler dev overrides:\n"
        f"  --var AUTH_MODE:jwt --var JWT_JWKS_URL:http://127.0.0.1:{JWKS_PORT}/jwks\n"
        f"  --var JWT_ISSUER:{ISSUER} --var JWT_AUDIENCE:{AUDIENCE} --var JWT_ALGORITHMS:RS256\n"
    )
    print(f"serving JWKS on http://127.0.0.1:{JWKS_PORT}/jwks (kid={KID}) — Ctrl-C to stop", flush=True)
    _serve_jwks(jwks_doc, JWKS_PORT)
