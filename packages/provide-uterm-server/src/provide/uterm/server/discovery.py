#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from provide.telemetry import get_logger
from provide.uterm.server import _http
from provide.uterm.server.egress import assert_webhook_target_allowed

logger = get_logger(__name__)


class NodeStatus(BaseModel):
    node_id: str
    active_sessions: int
    worker_count: int
    timestamp: float


@runtime_checkable
class DiscoveryProvider(Protocol):
    async def announce(self, status: NodeStatus) -> None:
        """Announce the node's status to the discovery service."""
        ...


class NoOpDiscoveryProvider(DiscoveryProvider):
    async def announce(self, status: NodeStatus) -> None:
        pass


class WebhookDiscoveryProvider(DiscoveryProvider):
    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 5.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def announce(self, status: NodeStatus) -> None:
        try:
            # SSRF guard: an EgressBlockedError (a ValueError) is caught by the
            # except below, so a blocked target is a logged best-effort no-op.
            await assert_webhook_target_allowed(self.url)
            async with _http.async_client(timeout=self.timeout) as client:
                headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
                await client.post(self.url, json=status.model_dump(), headers=headers)
        except Exception:
            logger.warning("node_registry_heartbeat_failed")
