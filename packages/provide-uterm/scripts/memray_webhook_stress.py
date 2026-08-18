# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Webhook dispatcher stress script for memray profiling.

Exercises the JSON-serialization + HMAC-signing + httpx2 POST hot path of
``WebhookManager._deliver`` for many events fanning out to multiple webhooks.
The network layer is short-circuited with ``httpx2.MockTransport`` so we only
measure dispatcher-side allocations, not socket / TLS noise.

Workload: 10 webhooks x 5_000 events.
Run via: python -m memray run -o webhook_stress.bin scripts/memray_webhook_stress.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx2

from provide.uterm.server import webhooks as _webhooks_mod
from provide.uterm.server.webhooks import WebhookConfig, WebhookManager

# Silence per-request HTTP logging — measurable allocations otherwise.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

NUM_WEBHOOKS = 10
NUM_EVENTS = 5_000


def _install_mock_transport() -> None:
    """Replace ``httpx2.AsyncClient`` in the webhooks module with a stub that
    returns ``200 OK`` immediately. Avoids real DNS / sockets / TLS so the
    measured allocations isolate the dispatcher's own work.
    """

    def _handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"")

    transport = httpx2.MockTransport(_handler)

    class _StubClient(httpx2.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    _webhooks_mod.httpx2.AsyncClient = _StubClient  # type: ignore[misc]


async def _fake_resolver(_hostname: str) -> tuple[str, ...]:
    # Public address that passes the SSRF guard. 8.8.8.8 is not private,
    # loopback, reserved, link-local, multicast, or unspecified, so the
    # ``_address_allowed`` check returns True without touching the network.
    return ("8.8.8.8",)


def _make_config(idx: int, signed: bool) -> WebhookConfig:
    """Build a webhook config without going through register() (which validates
    URLs via DNS). The dispatcher only reads fields off this object.
    """
    return WebhookConfig(
        webhook_id=f"wh-{idx:04d}",
        session_id="session-stress",
        url=f"https://example-{idx}.test/hook",
        event_types=None,
        pattern=None,
        secret="s3cret" if signed else None,
    )


async def main() -> None:
    _install_mock_transport()

    manager = WebhookManager(resolver=_fake_resolver, allow_loopback_destinations=False)
    configs = [_make_config(i, signed=(i % 2 == 0)) for i in range(NUM_WEBHOOKS)]

    # Realistic event payload — mirrors what EventBus delivers to subscribers.
    base_event: dict[str, Any] = {
        "worker_id": "worker-stress",
        "type": "snapshot",
        "data": {
            "screen": "user@host:~$ ls\nfile1 file2 file3\n" * 6,
            "screen_hash": "deadbeefcafebabe",
            "prompt_detected": True,
        },
    }

    for i in range(NUM_EVENTS):
        # Mutate one field so per-event JSON output isn't byte-identical (more
        # representative of real workloads).
        event = dict(base_event)
        event["seq"] = i
        for cfg in configs:
            await manager._deliver(cfg, event)


if __name__ == "__main__":
    asyncio.run(main())
