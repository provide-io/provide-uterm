#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validation tests for webhook registration inputs."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


@pytest.fixture()
def client() -> TestClient:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "S1",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    with TestClient(create_server_app(cfg)) as c:
        yield c


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/hook",
        # NOTE: the loopback destinations (localhost / 127.0.0.1 / ::1) are NOT
        # listed here. This fixture binds 127.0.0.1, and the effective loopback
        # permission is now ``allow_loopback_destinations or
        # _is_loopback_host(server.host)`` — a loopback-bound server accepts
        # loopback destinations because refusing them protected nothing (no
        # remote caller can reach that listener at all). The bind-dependent
        # loopback behaviour, on both loopback and routable binds, is covered by
        # tests/server/test_webhook_loopback_bind_default.py. Every refusal
        # below is bind-independent and must never be re-openable.
        "https://10.0.0.1/hook",
        "https://172.16.0.1/hook",
        "https://192.168.1.1/hook",
        "https://169.254.1.1/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://100.100.100.200/latest/meta-data",
        "https://0.0.0.0/hook",
        "https://224.0.0.1/hook",
    ],
)
def test_register_webhook_rejects_unsafe_url(client: TestClient, url: str) -> None:
    resp = client.post(
        "/api/sessions/s1/webhooks",
        json={"url": url},
        headers=ADMIN_H,
    )

    assert resp.status_code == 422


def test_register_webhook_rejects_malformed_regex(client: TestClient) -> None:
    resp = client.post(
        "/api/sessions/s1/webhooks",
        json={"url": "https://example.com/hook", "pattern": "["},
        headers=ADMIN_H,
    )

    assert resp.status_code == 422


@pytest.mark.parametrize("pattern", ["a" * 513, r"(a+)+$"])
def test_register_webhook_rejects_unsafe_regex(client: TestClient, pattern: str) -> None:
    resp = client.post(
        "/api/sessions/s1/webhooks",
        json={"url": "https://example.com/hook", "pattern": pattern},
        headers=ADMIN_H,
    )

    assert resp.status_code == 422


def test_register_webhook_accepts_public_https_url_and_valid_regex(client: TestClient) -> None:
    # The DNS-resolution SSRF guard is exercised separately; mock the resolver
    # so this happy-path test doesn't depend on live DNS being reachable from
    # the sandbox or returning a stable public IP.
    with patch(
        "provide.uterm.server.webhooks._resolve_hostname_sync",
        return_value=("93.184.216.34",),
    ):
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://hooks.example.com/uterm", "pattern": r"\$ "},
            headers=ADMIN_H,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://hooks.example.com/uterm"
    assert data["pattern"] == r"\$ "


def test_register_webhook_rejects_dns_name_resolving_to_metadata_ip(client: TestClient) -> None:
    """DNS rebinding-style host whose resolution returns 169.254.169.254 must be rejected."""
    with patch(
        "provide.uterm.server.webhooks._resolve_hostname_sync",
        return_value=("169.254.169.254",),
    ):
        resp = client.post(
            "/api/sessions/s1/webhooks",
            json={"url": "https://attacker.example.com/hook"},
            headers=ADMIN_H,
        )
    assert resp.status_code == 422


def test_register_webhook_allows_loopback_when_explicitly_configured() -> None:
    # This bind is loopback, so the permission would also be granted by the
    # bind alone; the assertion that the *key* is load-bearing lives in
    # test_webhook_loopback_bind_default.py (routable bind + key set).
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "webhooks": {"allow_loopback_destinations": True},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "S1",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )

    with TestClient(create_server_app(cfg)) as c:
        resp = c.post(
            "/api/sessions/s1/webhooks",
            json={"url": "http://127.0.0.1:9999/hook"},
            headers=ADMIN_H,
        )

    assert resp.status_code == 200
    assert resp.json()["url"] == "http://127.0.0.1:9999/hook"
