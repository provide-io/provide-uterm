#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RFC 6598 carrier-grade NAT space (``100.64.0.0/10``) is in the refusal set.

Both egress guards derive their deny list from CPython's address classifiers,
and CPython does **not** consider CGNAT space private::

    >>> ipaddress.ip_address("100.64.0.1").is_private
    False

That is a gap in the derivation, not a deliberate allowance.  CGNAT space
carries real infrastructure on carrier and container networks — it is where a
cloud provider parks its internal service mesh precisely *because* it is not
routable from the internet — so it is exactly the sort of address an SSRF pivot
wants, and every port blocks it explicitly (``conformance/EGRESS_GUARD.md`` §1).

Both derivation sites are covered here:

  * ``webhooks._address_allowed`` — unconditional, like every other member of
    the webhook refusal set except loopback.  No config key re-opens it, so the
    route cases below run on every bind/key combination.
  * ``egress._check_resolved_ip`` — gated on ``block_private``, like the rest of
    the private/loopback/reserved set it joins.  Connectors reaching internal
    hosts is their purpose; ``block_private`` is the multi-tenant posture, and
    CGNAT must follow that flag rather than becoming unconditional (only the
    cloud-metadata addresses are unconditional there).

The netmask itself is pinned, not just one address: the tests walk both edges of
the /10 (``100.64.0.0`` / ``100.127.255.255`` inside, ``100.63.255.255`` /
``100.128.0.0`` outside).  A guard written against ``100.64.0.0/16`` or
``100.0.0.0/8`` passes a single-address test and fails these.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed, assert_ip_allowed

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
OPERATOR_H = {"x-uterm-principal": "user1", "x-uterm-role": "operator"}

GUARD_MESSAGE = "webhook url host is not allowed"

# Inside 100.64.0.0/10 — the first address, an interior one, and the last.
CGNAT_INSIDE = ("100.64.0.0", "100.64.0.1", "100.100.0.1", "100.127.255.255")
# Immediately outside, either side.  Both are ordinary public unicast space and
# must stay reachable: a guard that swallows them is over-blocking, which is how
# an over-wide mask hides.
CGNAT_OUTSIDE = ("100.63.255.255", "100.128.0.0")


# ---------------------------------------------------------------------------
# Part 1 — the webhook guard, through create_server_app + the real route
# ---------------------------------------------------------------------------

# Every bind / key combination.  The key relaxes loopback and nothing else, so
# CGNAT must be refused in all four.
_EVERY_BIND = pytest.mark.parametrize(
    ("host", "allow_loopback"),
    [
        ("127.0.0.1", None),
        ("127.0.0.1", True),
        ("0.0.0.0", None),
        ("0.0.0.0", True),
    ],
)


def _webhook_config(*, host: str, allow_loopback: bool | None) -> Any:
    webhooks: dict[str, Any] = {} if allow_loopback is None else {"allow_loopback_destinations": allow_loopback}
    return config_from_mapping(
        {
            "server": {"host": host, "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                "trusted_proxy_ips": ["testclient"],
            },
            "webhooks": webhooks,
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


def _webhook_client(*, host: str, allow_loopback: bool | None) -> Iterator[TestClient]:
    with TestClient(create_server_app(_webhook_config(host=host, allow_loopback=allow_loopback))) as client:
        yield client


def _register(client: TestClient, url: str) -> Any:
    return client.post("/api/sessions/s1/webhooks", json={"url": url}, headers=ADMIN_H)


@_EVERY_BIND
@pytest.mark.parametrize("address", CGNAT_INSIDE)
def test_cgnat_webhook_destination_refused_on_every_bind(host: str, allow_loopback: bool | None, address: str) -> None:
    """No bind and no key re-opens CGNAT — it is not the conditional case."""
    for client in _webhook_client(host=host, allow_loopback=allow_loopback):
        resp = _register(client, f"https://{address}/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE


@_EVERY_BIND
@pytest.mark.parametrize("address", CGNAT_OUTSIDE)
def test_addresses_bordering_cgnat_still_register(host: str, allow_loopback: bool | None, address: str) -> None:
    """The netmask, not a prefix guess: one address either side of the /10.

    ``100.63.255.255`` and ``100.128.0.0`` are ordinary public addresses.  A
    guard written against ``100.0.0.0/8`` — the shape a careless fix takes —
    refuses both and fails here while still passing every positive case above.
    """
    for client in _webhook_client(host=host, allow_loopback=allow_loopback):
        resp = _register(client, f"https://{address}/hook")
        assert resp.status_code == 200, resp.text


def test_cgnat_hostname_refused_at_registration() -> None:
    """A DNS name is judged by what it resolves to, CGNAT included."""
    for client in _webhook_client(host="127.0.0.1", allow_loopback=None):
        with patch("provide.uterm.server.webhooks._resolve_hostname_sync", return_value=("100.64.0.1",)):
            resp = _register(client, "https://mesh.example.com/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE


def _post_mock() -> tuple[MagicMock, AsyncMock]:
    """Return (stub ``httpx.AsyncClient`` class, its ``post`` AsyncMock)."""
    response = MagicMock(name="response")
    response.is_success = True
    response.status_code = 200
    post = AsyncMock(name="post", return_value=response)
    http = MagicMock(name="client")
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.post = post
    return MagicMock(name="AsyncClient", return_value=http), post


@pytest.mark.parametrize(("resolved", "delivers"), [("100.64.0.1", False), ("93.184.216.34", True)])
def test_cgnat_rebind_refused_at_delivery(resolved: str, delivers: bool) -> None:
    """Delivery re-resolves, so a rebind into CGNAT after registration is caught.

    Registration is the only route this guard has, so the delivery half is
    driven on the live app's own ``WebhookManager`` with its resolver swung
    after the fact — the post-registration DNS rebind that the delivery-time
    re-check exists for.  The public resolution is the control case: it proves
    the refusal is the new CGNAT branch and not the stubbing.
    """
    for client in _webhook_client(host="127.0.0.1", allow_loopback=None):
        with patch("provide.uterm.server.webhooks._resolve_hostname_sync", return_value=("93.184.216.34",)):
            resp = _register(client, "https://mesh.example.com/hook")
        assert resp.status_code == 200, resp.text
        manager = client.app.state.uterm_webhooks  # type: ignore[union-attr]
        metrics = client.app.state.uterm_metrics  # type: ignore[union-attr]
        cfg = manager.get_webhook(resp.json()["webhook_id"])

        client_cls, post = _post_mock()
        with (
            patch("httpx.AsyncClient", client_cls),
            patch.object(manager, "_resolver", lambda _host: (resolved,)),
        ):
            asyncio.run(manager._deliver(cfg, {"type": "snapshot"}))

        assert post.await_count == (1 if delivers else 0)
        assert metrics.get("webhook_delivery_blocked_total", 0) == (0 if delivers else 1)


# ---------------------------------------------------------------------------
# Part 2 — the connector guard, which keeps its ``block_private`` gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", CGNAT_INSIDE)
async def test_cgnat_connector_target_blocked_when_private_blocked(address: str) -> None:
    """CGNAT joins the private set, and so is refused under the strict posture."""
    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed(address, block_private=True)


@pytest.mark.parametrize("address", CGNAT_INSIDE)
async def test_cgnat_connector_target_allowed_by_default(address: str) -> None:
    """CGNAT follows ``block_private``; it does NOT become unconditional.

    The default connector posture is to permit internal hosts — that is what a
    connector is *for* — and only the cloud-metadata addresses are refused
    regardless of the flag.  Making CGNAT unconditional here would silently
    break every deployment whose terminals live behind a carrier NAT, which is
    a different (and unmandated) policy change from the one §1 asks for.
    """
    await assert_connector_target_allowed(address, block_private=False)


@pytest.mark.parametrize("address", CGNAT_OUTSIDE)
@pytest.mark.parametrize("block_private", [False, True])
async def test_addresses_bordering_cgnat_reach_connectors(address: str, block_private: bool) -> None:
    """The /10 edges again, on the connector guard, under both postures."""
    await assert_connector_target_allowed(address, block_private=block_private)


@pytest.mark.parametrize("address", CGNAT_INSIDE)
def test_cgnat_peer_ip_blocked_when_private_blocked(address: str) -> None:
    """The post-connect peer-IP check (M3 rebinding mitigation) agrees."""
    with pytest.raises(EgressBlockedError, match="internal"):
        assert_ip_allowed(address, block_private=True)


@pytest.mark.parametrize("address", CGNAT_OUTSIDE)
def test_peer_ip_bordering_cgnat_allowed(address: str) -> None:
    assert_ip_allowed(address, block_private=True)


async def test_cgnat_connector_hostname_blocked_when_private_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name resolving into CGNAT is refused under the strict posture."""
    from provide.uterm.server import egress as egress_mod

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=("100.64.0.1",)))
    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("mesh.internal.example", block_private=True)


# ---------------------------------------------------------------------------
# Part 3 — the connector guard through the real /api/connect route
# ---------------------------------------------------------------------------


@pytest.fixture
def strict_connect_client() -> TestClient:
    """A server in the multi-tenant posture (``block_private_connector_targets``)."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.security.block_private_connector_targets = True
    config.recording.directory = Path(tempfile.mkdtemp())
    return TestClient(create_server_app(config))


@pytest.mark.parametrize("address", CGNAT_INSIDE)
def test_quick_connect_to_cgnat_returns_422(strict_connect_client: TestClient, address: str) -> None:
    resp = strict_connect_client.post(
        "/api/connect",
        json={"connector_type": "ssh", "host": address, "port": 22},
        headers=OPERATOR_H,
    )
    assert resp.status_code == 422, resp.text
    assert "internal" in resp.json()["detail"].lower()
