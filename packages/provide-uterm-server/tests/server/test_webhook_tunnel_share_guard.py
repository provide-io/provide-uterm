#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Delivery-time refusal of loopback webhook destinations while tunnel-shared.

A loopback bind grants the loopback-destination permission (see
test_webhook_loopback_bind_default.py) because "bound to loopback" implies "only
local callers exist".  Issuing a tunnel share breaks that implication: the share
relays a loopback-bound server to remote viewers, so a loopback webhook
destination becomes a reachable-from-outside SSRF pivot again.

Tunnel shares are created at runtime (``POST /api/tunnels``), so this is only
knowable at delivery time — it cannot be folded into the load-time default.

Section A drives the real ``create_server_app`` factory: the share is created
through the real ``POST /api/tunnels`` route and the webhook through the real
registration route, so the wiring from live tunnel-token state to the delivery
guard is under test.  Only the final network hop is stubbed (``httpx2``): the
EventBus -> ``_deliver`` hand-off is covered elsewhere and exercises no part of
this guard, whereas a real HTTP receiver would add timing flake for no extra
coverage.

Section B is unit-level for the states no route can produce: the exact expiry
boundary, a share record with an unusable ``expires_at``, an unwired manager,
and a non-loopback-but-blocked destination sitting in the registry (only
reachable via post-registration DNS rebinding, since registration refuses it).

Section C pins the *order* the two delivery guards run in (EGRESS_GUARD.md §4).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.webhooks import _MAX_BLOCKED_DELIVERIES, WebhookConfig, WebhookManager

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}

BLOCK_METRIC = "webhook_delivery_blocked_tunnel_total"
SSRF_METRIC = "webhook_delivery_blocked_total"
UNREG_METRIC = "webhook_auto_unregistered_total"

LOOPBACK_URL = "http://127.0.0.1:9999/hook"
PUBLIC_IP_URL = "https://93.184.216.34/hook"


def _config(*, host: str = "127.0.0.1", allow_loopback: bool | None = None) -> Any:
    return config_from_mapping(
        {
            # Loopback bind (the default here): loopback webhook destinations are
            # permitted by the bind-derived default, so any refusal below is the
            # share guard. Section C overrides *host* to get the opposite posture.
            "server": {"host": host, "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                # Required by the header-mode validator on a non-loopback bind.
                # The TestClient's transport peer reports itself as "testclient".
                "trusted_proxy_ips": ["testclient"],
            },
            "webhooks": {} if allow_loopback is None else {"allow_loopback_destinations": allow_loopback},
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


def _post_mock() -> tuple[MagicMock, AsyncMock]:
    """Return (stub ``httpx2.AsyncClient`` class, its ``post`` AsyncMock)."""
    response = MagicMock(name="response")
    response.is_success = True
    response.status_code = 200
    post = AsyncMock(name="post", return_value=response)
    client = MagicMock(name="client")
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = post
    return MagicMock(name="AsyncClient", return_value=client), post


class _Harness:
    """A live app + TestClient whose deliveries can be driven one at a time."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.app = client.app
        self.metrics: dict[str, int] = self.app.state.uterm_metrics  # type: ignore[union-attr]
        self.manager: WebhookManager = self.app.state.uterm_webhooks  # type: ignore[union-attr]
        self.tunnel_tokens: dict[str, dict[str, Any]] = self.app.state.uterm_tunnel_tokens  # type: ignore[union-attr]

    def create_tunnel(self) -> str:
        resp = self.client.post("/api/tunnels", json={"display_name": "shared"}, headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        return str(resp.json()["tunnel_id"])

    def register(self, session_id: str, url: str) -> WebhookConfig:
        resp = self.client.post(f"/api/sessions/{session_id}/webhooks", json={"url": url}, headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        cfg = self.manager.get_webhook(str(resp.json()["webhook_id"]))
        assert cfg is not None
        return cfg

    def deliver(self, cfg: WebhookConfig, *, resolved: tuple[str, ...] | None = None) -> AsyncMock:
        """Run one delivery on a private loop; return the stubbed ``post`` mock.

        A private ``asyncio.run`` loop is used deliberately: the TestClient's app
        keeps running on its own portal thread (so the live tunnel state the
        guard reads is still the app's real state), while ``_deliver`` itself
        needs no app loop.
        """
        client_cls, post = _post_mock()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("httpx2.AsyncClient", client_cls))
            stack.enter_context(patch("provide.uterm.server.webhooks.asyncio.sleep", AsyncMock()))
            if resolved is not None:
                stack.enter_context(patch.object(self.manager, "_resolver", lambda _host: resolved))
            asyncio.run(self.manager._deliver(cfg, {"type": "snapshot"}))
        return post

    def counter(self, name: str) -> int:
        return self.metrics.get(name, 0)


@pytest.fixture
def app_harness() -> Any:
    with TestClient(create_server_app(_config())) as client:
        yield _Harness(client)


# ===========================================================================
# Section A — end to end through create_server_app + POST /api/tunnels
# ===========================================================================


def test_loopback_delivery_refused_while_tunnel_shared(app_harness: _Harness) -> None:
    tunnel_id = app_harness.create_tunnel()
    cfg = app_harness.register(tunnel_id, LOOPBACK_URL)

    app_harness.deliver(cfg).assert_not_awaited()

    assert app_harness.counter(BLOCK_METRIC) == 1


def test_loopback_delivery_proceeds_once_share_expires(app_harness: _Harness) -> None:
    tunnel_id = app_harness.create_tunnel()
    cfg = app_harness.register(tunnel_id, LOOPBACK_URL)
    # Expire the share in place. The background sweep only runs once a minute,
    # so an expired-but-not-yet-swept record MUST read as expired at delivery
    # time — otherwise one share closes the guard until the next sweep tick.
    app_harness.tunnel_tokens[tunnel_id]["expires_at"] = time.time() - 1.0

    app_harness.deliver(cfg).assert_awaited_once()

    assert app_harness.counter(BLOCK_METRIC) == 0


def test_loopback_delivery_proceeds_once_share_revoked(app_harness: _Harness) -> None:
    tunnel_id = app_harness.create_tunnel()
    cfg = app_harness.register(tunnel_id, LOOPBACK_URL)
    revoke = app_harness.client.delete(f"/api/tunnels/{tunnel_id}/tokens", headers=ADMIN_H)
    assert revoke.status_code == 200, revoke.text

    app_harness.deliver(cfg).assert_awaited_once()

    assert app_harness.counter(BLOCK_METRIC) == 0


def test_loopback_delivery_proceeds_for_session_never_shared(app_harness: _Harness) -> None:
    cfg = app_harness.register("s1", LOOPBACK_URL)

    app_harness.deliver(cfg).assert_awaited_once()

    assert app_harness.counter(BLOCK_METRIC) == 0


def test_share_does_not_gag_other_sessions(app_harness: _Harness) -> None:
    """The guard is per-session: sharing one tunnel must not gag every webhook."""
    app_harness.create_tunnel()
    cfg = app_harness.register("s1", LOOPBACK_URL)

    app_harness.deliver(cfg).assert_awaited_once()

    assert app_harness.counter(BLOCK_METRIC) == 0


def test_public_destination_delivers_while_tunnel_shared(app_harness: _Harness) -> None:
    """Only *loopback* destinations are refused; a share is not a blanket gag."""
    tunnel_id = app_harness.create_tunnel()
    cfg = app_harness.register(tunnel_id, PUBLIC_IP_URL)

    app_harness.deliver(cfg).assert_awaited_once()

    assert app_harness.counter(BLOCK_METRIC) == 0


def test_hostname_resolving_to_loopback_refused_while_tunnel_shared(app_harness: _Harness) -> None:
    """A DNS name is judged by what it resolves to, at delivery time too."""
    tunnel_id = app_harness.create_tunnel()
    with patch("provide.uterm.server.webhooks._resolve_hostname_sync", return_value=("127.0.0.1",)):
        cfg = app_harness.register(tunnel_id, "https://dev.example.com/hook")

    app_harness.deliver(cfg, resolved=("127.0.0.1",)).assert_not_awaited()

    assert app_harness.counter(BLOCK_METRIC) == 1


def test_share_refusal_never_auto_unregisters_the_webhook(app_harness: _Harness) -> None:
    """A share is transient, so its refusals must not feed the SSRF kill switch.

    ``_MAX_BLOCKED_DELIVERIES`` exists to retire a webhook whose destination has
    permanently gone bad. A tunnel share un-shares itself on expiry, so counting
    its refusals there would let a few minutes of sharing silently delete a
    perfectly good webhook.
    """
    tunnel_id = app_harness.create_tunnel()
    cfg = app_harness.register(tunnel_id, LOOPBACK_URL)

    for _ in range(5):  # well past _MAX_BLOCKED_DELIVERIES == 3
        app_harness.deliver(cfg).assert_not_awaited()

    assert app_harness.counter(BLOCK_METRIC) == 5
    assert app_harness.counter(SSRF_METRIC) == 0
    assert app_harness.counter(UNREG_METRIC) == 0
    assert app_harness.manager.get_webhook(cfg.webhook_id) is not None
    # And once the share lapses the webhook delivers normally again.
    app_harness.tunnel_tokens[tunnel_id]["expires_at"] = time.time() - 1.0
    app_harness.deliver(cfg).assert_awaited_once()


def test_block_counter_is_preseeded_at_zero(app_harness: _Harness) -> None:
    """Operators must see the counter before it first fires, like its neighbours."""
    assert app_harness.metrics[BLOCK_METRIC] == 0


# ===========================================================================
# Section B — unit level: states no route can produce
# ===========================================================================


def _bare_manager(
    tunnel_tokens: Any,
    *,
    metric: Any = None,
    resolved: tuple[str, ...] = ("127.0.0.1",),
) -> WebhookManager:
    return WebhookManager(
        resolver=lambda _host: resolved,
        allow_loopback_destinations=True,
        on_metric=metric,
        tunnel_tokens=tunnel_tokens,
    )


def _bare_cfg(url: str = LOOPBACK_URL) -> WebhookConfig:
    return WebhookConfig(
        webhook_id="wh1",
        session_id="s1",
        url=url,
        event_types=None,
        pattern=None,
        secret=None,
    )


async def _run(mgr: WebhookManager, cfg: WebhookConfig) -> AsyncMock:
    client_cls, post = _post_mock()
    with patch("httpx2.AsyncClient", client_cls), patch("provide.uterm.server.webhooks.asyncio.sleep", AsyncMock()):
        await mgr._deliver(cfg, {"type": "snapshot"})
    return post


@pytest.mark.parametrize(
    ("offset", "still_shared"),
    [
        (1.0, True),  # live
        (0.0, False),  # exact boundary: now == expires_at is ALREADY expired
        (-1.0, False),  # expired
    ],
)
async def test_expiry_boundary(offset: float, still_shared: bool) -> None:
    """``now == expires_at`` is already expired — pins ``<`` against ``<=``.

    The boundary direction is fixed by conformance/EGRESS_GUARD.md §4 so all
    four ports agree; note it is the opposite of the ``now > expires_at``
    convention in ``sweep_expired_tunnel_tokens``.

    Unit-level because the exact boundary cannot be hit through the route (the
    route stamps ``expires_at`` from its own ``time.time()``), and it needs the
    guard's clock pinned to the same instant rather than racing the real one.
    """
    metric = MagicMock()
    now = time.time()
    mgr = _bare_manager({"s1": {"expires_at": now + offset}}, metric=metric)
    with patch("provide.uterm.server.webhooks.time.time", return_value=now):
        post = await _run(mgr, _bare_cfg())

    assert post.await_count == (0 if still_shared else 1)
    fired = [call for call in metric.call_args_list if call[0][0] == BLOCK_METRIC]
    assert bool(fired) is still_shared


@pytest.mark.parametrize("state", [{}, {"expires_at": None}, {"expires_at": "soon"}])
async def test_share_record_without_usable_expiry_fails_closed(state: dict[str, Any]) -> None:
    """An unparseable expiry cannot be proven lapsed, so the guard stays shut."""
    mgr = _bare_manager({"s1": state})

    post = await _run(mgr, _bare_cfg())

    assert post.await_count == 0


async def test_unwired_manager_never_refuses() -> None:
    """A manager constructed without the share store (embedders, unit tests)."""
    mgr = _bare_manager(None)

    (await _run(mgr, _bare_cfg())).assert_awaited_once()


async def test_shared_session_with_private_destination_uses_the_ssrf_path() -> None:
    """A blocked-for-another-reason destination keeps the auto-unregister path.

    Registration refuses a private destination, so the only way one is in the
    registry at delivery time is a post-registration DNS rebind — modelled here
    by seeding the config directly. Attributing such a refusal to the share
    guard would disable the kill switch for exactly the case it exists for, so
    it must still take the SSRF path.
    """
    metric = MagicMock()
    mgr = _bare_manager(
        {"s1": {"expires_at": time.time() + 60.0}},
        metric=metric,
        resolved=("10.0.0.7",),
    )
    cfg = _bare_cfg("https://rebound.example.com/hook")
    mgr._webhooks[cfg.webhook_id] = cfg

    for _ in range(3):  # reach _MAX_BLOCKED_DELIVERIES
        assert (await _run(mgr, cfg)).await_count == 0
    await asyncio.sleep(0)  # let the background unregister task run

    names = [call[0][0] for call in metric.call_args_list]
    assert BLOCK_METRIC not in names
    assert names.count(SSRF_METRIC) == 3
    assert UNREG_METRIC in names
    assert mgr.get_webhook(cfg.webhook_id) is None


# ===========================================================================
# Section C — the order the two delivery guards run in (EGRESS_GUARD.md §4)
# ===========================================================================
#
# Destination safety runs FIRST, the share guard SECOND. The two orders differ
# only in one state: the configuration refuses loopback, a share is live, and
# the destination is loopback-only. Such a destination can never deliver under
# the current configuration, so it must land on the generic counter that
# eventually retires the webhook — reporting it as a share refusal (which is
# deliberately exempt from the kill switch) would keep a permanently-dead
# webhook alive forever.


@pytest.fixture
def routable_harness() -> Any:
    """A routable bind with no key: the effective loopback permission is False."""
    with TestClient(create_server_app(_config(host="0.0.0.0"))) as client:
        yield _Harness(client)


@pytest.fixture
def routable_opted_in_harness() -> Any:
    """A routable bind WITH the key: loopback is permitted, so the share guard owns it."""
    with TestClient(create_server_app(_config(host="0.0.0.0", allow_loopback=True))) as client:
        yield _Harness(client)


def test_config_refused_loopback_is_an_unsafe_destination_not_a_share_refusal(
    routable_harness: _Harness,
) -> None:
    """Config refuses loopback + share live + loopback destination → SSRF path.

    Registration is driven through the real route with the name resolving to a
    public address, then rebound to loopback at delivery: with the key unset on
    a routable bind, registration would (correctly) refuse a loopback literal
    outright, so a post-registration rebind is the only way this state is
    reachable — and it is exactly the state EGRESS_GUARD.md §4 pins.
    """
    tunnel_id = routable_harness.create_tunnel()
    with patch("provide.uterm.server.webhooks._resolve_hostname_sync", return_value=("93.184.216.34",)):
        cfg = routable_harness.register(tunnel_id, "https://rebound.example.com/hook")

    for _ in range(_MAX_BLOCKED_DELIVERIES):
        routable_harness.deliver(cfg, resolved=("127.0.0.1",)).assert_not_awaited()

    # The share counter is deliberately exempt from the kill switch, so a
    # refusal booked there would never retire this webhook.
    assert routable_harness.counter(BLOCK_METRIC) == 0
    assert routable_harness.counter(SSRF_METRIC) == _MAX_BLOCKED_DELIVERIES
    assert routable_harness.counter(UNREG_METRIC) == 1


def test_permitted_loopback_while_shared_is_still_a_share_refusal(
    routable_opted_in_harness: _Harness,
) -> None:
    """The reorder must not swallow the share guard where it still applies.

    Same routable bind, but the operator opted in: the destination is fine by
    the configuration, so destination safety passes and the share guard — whose
    whole purpose is destinations that would otherwise be fine — owns the
    refusal, on its own counter and clear of the kill switch.
    """
    tunnel_id = routable_opted_in_harness.create_tunnel()
    cfg = routable_opted_in_harness.register(tunnel_id, LOOPBACK_URL)

    for _ in range(_MAX_BLOCKED_DELIVERIES):
        routable_opted_in_harness.deliver(cfg).assert_not_awaited()

    assert routable_opted_in_harness.counter(BLOCK_METRIC) == _MAX_BLOCKED_DELIVERIES
    assert routable_opted_in_harness.counter(SSRF_METRIC) == 0
    assert routable_opted_in_harness.counter(UNREG_METRIC) == 0
    assert routable_opted_in_harness.manager.get_webhook(cfg.webhook_id) is not None
