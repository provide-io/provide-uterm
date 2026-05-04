"""E2E smoke tests against a live pywrangler dev server.

These tests are skipped by default.  Run with:
    uv run pytest -m e2e
or:
    E2E=1 uv run pytest packages/provide-uterm-cloudflare/tests/

The ``wrangler_server`` fixture (conftest.py) starts ``pywrangler dev`` and
waits for it to be healthy before these tests run.
"""

from __future__ import annotations

import json
import urllib.request

import pytest


def _get(base: str, path: str) -> tuple[int, dict]:
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.request.HTTPError as exc:
        return exc.code, {}


@pytest.mark.e2e
def test_health_endpoint(wrangler_server: str) -> None:
    status, body = _get(wrangler_server, "/api/health")
    # 200 in AUTH_MODE=dev (local pywrangler); 401/403 in jwt/CF-Access mode.
    if status == 200:
        assert body.get("ok") is True
        assert body.get("service") == "provide-uterm-cloudflare"
    else:
        assert status in {401, 403}, f"unexpected status {status}"


@pytest.mark.e2e
def test_sessions_endpoint_returns_list(wrangler_server: str) -> None:
    status, body = _get(wrangler_server, "/api/sessions")
    # 200 + list in dev mode; 401/403 when auth is required.
    if status == 200:
        assert isinstance(body, list)
    else:
        assert status in {401, 403}, f"unexpected status {status}"


@pytest.mark.e2e
def test_unknown_route_returns_404(wrangler_server: str) -> None:
    status, _body = _get(wrangler_server, "/api/does-not-exist")
    # 404 in dev mode (auth passes, route not found);
    # 401/403 when auth is required (blocks before routing).
    assert status in {401, 403, 404}, f"unexpected status {status}"


@pytest.mark.e2e
def test_hijack_acquire_requires_worker_id(wrangler_server: str) -> None:
    # /worker/{id}/hijack/acquire with no worker session returns 409 or 403
    # depending on auth config; either way it must not be 500.
    import urllib.error

    url = f"{wrangler_server}/worker/test-agent/hijack/acquire"
    data = json.dumps({"owner": "e2e-test", "lease_s": 10}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    # 200 = acquired, 401 = jwt required, 403 = forbidden, 409 = no worker
    assert status in {200, 401, 403, 409}, f"unexpected status {status}"
