#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for WebhookDiscoveryProvider, including the egress SSRF guard (L28)."""

from __future__ import annotations

import httpx
import pytest
import respx

from provide.uterm.server.discovery import (
    NodeStatus,
    NoOpDiscoveryProvider,
    WebhookDiscoveryProvider,
)


def _status() -> NodeStatus:
    return NodeStatus(node_id="node-1", active_sessions=2, worker_count=1, timestamp=123.0)


@pytest.mark.asyncio
async def test_noop_discovery_announce_is_noop() -> None:
    """NoOpDiscoveryProvider.announce never raises and makes no request."""
    await NoOpDiscoveryProvider().announce(_status())


@pytest.mark.asyncio
async def test_webhook_discovery_announce_posts_status() -> None:
    """A benign URL posts the serialized NodeStatus with the bearer header."""
    url = "https://registry.example.com/announce"
    secret = "uterm-discovery-secret-32-byte-minimum-x"  # pragma: allowlist secret
    provider = WebhookDiscoveryProvider(url, secret=secret)

    async with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(200))
        await provider.announce(_status())
        assert route.called
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {secret}"


@pytest.mark.asyncio
async def test_webhook_discovery_announce_swallows_http_error() -> None:
    """A transport error is swallowed (best-effort heartbeat)."""
    url = "https://registry.example.com/announce"
    provider = WebhookDiscoveryProvider(url)

    async with respx.mock:
        respx.post(url).mock(side_effect=httpx.ConnectError("refused"))
        # Must not raise.
        await provider.announce(_status())


# ---------------------------------------------------------------------------
# L28: outbound discovery webhook honours the egress SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_discovery_announce_metadata_url_not_sent() -> None:
    """announce to a cloud-metadata IP must be blocked by the egress guard and
    degrade gracefully (no HTTP request made, no exception raised)."""
    url = "http://169.254.169.254/announce"
    provider = WebhookDiscoveryProvider(url)

    async with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(200))
        await provider.announce(_status())
        assert not route.called
