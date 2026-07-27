#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared auth-header helpers for the CF Worker e2e tests.

The worker is jwt-only (the ``AUTH_MODE=dev`` bypass was removed), so each
request must carry the credential the worker expects for that route:

* **Principal routes** (HTTP API, browser WS) — a JWT the worker's
  ``decode_jwt`` accepts, supplied via ``CF_E2E_JWT``. Mint one with
  :mod:`cf_jwt_harness` against a test JWKS, or use a real CF Access token.
* **Worker WS routes** (``/ws/worker/...``) — the worker bearer token (the
  ``global_bearer`` edge boundary), supplied via ``CF_WORKER_BEARER_TOKEN``,
  which must equal the worker's configured ``WORKER_BEARER_TOKEN`` secret.
* **Real CF over https** behind Access — also the CF Access service-token pair.

Without these the authenticated routes 401. See the module docstrings in this
package's ``cf_*`` test harnesses for wiring a local worker.
"""

from __future__ import annotations

import os

CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
WORKER_BEARER_TOKEN = os.environ.get("CF_WORKER_BEARER_TOKEN", "")
E2E_JWT = os.environ.get("CF_E2E_JWT", "")


def _cf_access(target: str) -> dict[str, str]:
    """CF Access service-token headers — only for real CF (https/wss)."""
    if target.startswith(("http://", "ws://")):
        return {}
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        return {
            "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
            "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
        }
    return {}


def http_auth_headers(url: str = "") -> dict[str, str]:
    """Headers for an authenticated HTTP request: principal JWT + CF Access."""
    headers = _cf_access(url)
    if E2E_JWT:
        headers["Authorization"] = f"Bearer {E2E_JWT}"
    return headers


def ws_auth_headers(uri: str = "") -> dict[str, str]:
    """Headers for a WS upgrade.

    ``/ws/worker/...`` connections authenticate as the worker (bearer token);
    every other upgrade is a principal connection and uses the JWT.
    """
    headers = _cf_access(uri)
    if "/ws/worker/" in uri:
        if WORKER_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {WORKER_BEARER_TOKEN}"
    elif E2E_JWT:
        headers["Authorization"] = f"Bearer {E2E_JWT}"
    return headers
