#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for WebhookManager."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.webhooks import WebhookConfig, WebhookManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str = "snapshot", screen: str = "$ test") -> dict[str, Any]:
    return {"type": event_type, "seq": 1, "ts": time.time(), "data": {"screen": screen}}


def _make_manager(resolved_ips: tuple[str, ...] = ("93.184.216.34",)) -> WebhookManager:
    return WebhookManager(resolver=lambda _hostname: resolved_ips)


async def _make_bus_with_worker(session_id: str = "s1") -> tuple[EventBus, TermHub]:
    bus = EventBus()
    hub = TermHub(event_bus=bus)
    await hub._get(session_id)
    return bus, hub


# ---------------------------------------------------------------------------
# register / unregister / list / get
# ---------------------------------------------------------------------------


async def test_register_returns_config() -> None:
    manager = _make_manager()
    cfg = await manager.register("s1", "https://example.com/hook")
    assert isinstance(cfg, WebhookConfig)
    assert cfg.session_id == "s1"
    assert cfg.url == "https://example.com/hook"
    assert cfg.event_types is None
    assert cfg.pattern is None
    assert cfg.secret is None
    await manager.shutdown()


async def test_register_with_all_options() -> None:
    manager = _make_manager()
    cfg = await manager.register(
        "s1",
        "https://example.com/hook",
        event_types=["snapshot", "hijack_acquired"],
        pattern=r"\$\s",
        secret="mysecret",
    )
    assert cfg.event_types == frozenset({"snapshot", "hijack_acquired"})
    assert cfg.pattern == r"\$\s"
    assert cfg.secret == "mysecret"
    await manager.shutdown()


async def test_unregister_returns_true_when_found() -> None:
    manager = _make_manager()
    cfg = await manager.register("s1", "https://example.com/hook")
    result = await manager.unregister(cfg.webhook_id)
    assert result is True
    await manager.shutdown()


async def test_unregister_when_task_already_done() -> None:
    """Unregister after the delivery task has already completed (no-op branch)."""
    manager = _make_manager()
    # Register without event_bus → task exits immediately
    cfg = await manager.register("s1", "https://example.com/hook", event_bus=None)
    task = manager._tasks[cfg.webhook_id]
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    # Unregister a completed task — should hit the task.done() True branch
    result = await manager.unregister(cfg.webhook_id)
    assert result is True


async def test_unregister_returns_false_when_not_found() -> None:
    manager = _make_manager()
    result = await manager.unregister("nonexistent")
    assert result is False


async def test_list_webhooks_filters_by_session() -> None:
    manager = _make_manager()
    cfg1 = await manager.register("s1", "https://example.com/a")
    cfg2 = await manager.register("s1", "https://example.com/b")
    await manager.register("s2", "https://example.com/c")
    result = manager.list_webhooks("s1")
    ids = {c.webhook_id for c in result}
    assert cfg1.webhook_id in ids
    assert cfg2.webhook_id in ids
    assert len(result) == 2
    await manager.shutdown()


async def test_get_webhook() -> None:
    manager = _make_manager()
    cfg = await manager.register("s1", "https://example.com/hook")
    assert manager.get_webhook(cfg.webhook_id) is cfg
    assert manager.get_webhook("nonexistent") is None
    await manager.shutdown()


async def test_shutdown_clears_registry() -> None:
    manager = _make_manager()
    await manager.register("s1", "https://example.com/hook")
    await manager.shutdown()
    assert manager.list_webhooks("s1") == []


# ---------------------------------------------------------------------------
# Delivery loop — no EventBus (no-op)
# ---------------------------------------------------------------------------


async def test_delivery_loop_no_event_bus_exits_immediately() -> None:
    manager = _make_manager()
    cfg = await manager.register("s1", "https://example.com/hook", event_bus=None)
    # Task should complete quickly since event_bus is None
    task = manager._tasks[cfg.webhook_id]
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Delivery — success path
# ---------------------------------------------------------------------------


async def test_deliver_posts_to_url() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    received: list[dict[str, Any]] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", b"")
        received.append(json.loads(body))
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ hello"})
        await asyncio.sleep(0.2)

    assert len(received) >= 1
    payload = received[0]
    assert payload["session_id"] == "s1"
    assert payload["event"]["type"] == "snapshot"
    assert "webhook_id" in payload
    assert "timestamp" in payload
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Delivery — HMAC signing
# ---------------------------------------------------------------------------


async def test_deliver_adds_hmac_signature() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    captured_headers: list[dict[str, str]] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.append(dict(kwargs.get("headers", {})))
        resp = MagicMock()
        resp.is_success = True
        return resp

    secret = "uterm-test-secret-32-byte-minimum-key"
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/hook", secret=secret, event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ signed"})
        await asyncio.sleep(0.2)

    assert len(captured_headers) >= 1
    sig_header = captured_headers[0].get("X-Uterm-Signature", "")
    assert sig_header.startswith("sha256=")
    assert "X-Uterm-Timestamp" in captured_headers[0]
    assert "X-Webhook-Secret" not in captured_headers[0]
    await manager.shutdown()


async def test_deliver_no_signature_when_no_secret() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    captured_headers: list[dict[str, str]] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.append(dict(kwargs.get("headers", {})))
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ unsigned"})
        await asyncio.sleep(0.2)

    assert len(captured_headers) >= 1
    assert "X-Uterm-Signature" not in captured_headers[0]
    await manager.shutdown()


async def test_hmac_signature_is_correct() -> None:
    """Verify the HMAC signature can be independently re-verified (timestamped scheme)."""
    from provide.uterm.server.webhook_signing import build_webhook_signature

    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    captured: list[tuple[bytes, str, str]] = []  # (body, signature, timestamp)

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", b"")
        headers = kwargs.get("headers", {})
        sig = headers.get("X-Uterm-Signature", "")
        ts = headers.get("X-Uterm-Timestamp", "")
        captured.append((body, sig, ts))
        resp = MagicMock()
        resp.is_success = True
        return resp

    secret = "uterm-test-secret-32-byte-minimum-key"
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/hook", secret=secret, event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ check"})
        await asyncio.sleep(0.2)

    assert captured
    body, sig_header, ts_header = captured[0]
    assert ts_header != ""
    expected = build_webhook_signature(secret, body, ts_header)
    assert sig_header == expected
    # X-Webhook-Secret must not appear in delivery headers.
    assert "X-Webhook-Secret" not in captured[0]
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Delivery — retry on 5xx
# ---------------------------------------------------------------------------


async def test_deliver_retries_on_5xx() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    call_count = 0

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.is_success = call_count >= 3  # succeed on 3rd attempt
        resp.status_code = 500 if call_count < 3 else 200
        return resp

    # Patch _RETRY_DELAYS to near-zero so retries are fast without affecting
    # the test's own asyncio.sleep calls.
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)),
        patch("provide.uterm.server.webhooks._RETRY_DELAYS", (0.001, 0.001, 0.001)),
    ):
        await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ retry"})
        await asyncio.sleep(0.3)

    assert call_count == 3
    await manager.shutdown()


async def test_deliver_gives_up_after_max_retries() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    call_count = 0

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 503
        return resp

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)),
        patch("provide.uterm.server.webhooks._RETRY_DELAYS", (0.001, 0.001, 0.001)),
    ):
        await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ fail"})
        await asyncio.sleep(1.0)

    # for attempt, delay in enumerate((*_RETRY_DELAYS, None)) → 4 iterations
    assert call_count == 4
    await manager.shutdown()


async def test_deliver_retries_on_network_error() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    call_count = 0

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("refused")
        resp = MagicMock()
        resp.is_success = True
        return resp

    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)),
        patch("provide.uterm.server.webhooks._RETRY_DELAYS", (0.001, 0.001, 0.001)),
    ):
        await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ error"})
        await asyncio.sleep(0.3)

    assert call_count == 3
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Delivery — SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
    ],
)
async def test_deliver_rejects_dns_names_resolving_to_blocked_addresses(blocked_ip: str) -> None:
    manager = _make_manager((blocked_ip,))
    cfg = WebhookConfig(
        webhook_id="wh1",
        session_id="s1",
        url="https://webhook.example/hook",
        event_types=None,
        pattern=None,
        secret=None,
    )

    post = AsyncMock()
    with patch("httpx.AsyncClient.post", new=post):
        await manager._deliver(cfg, _make_event())

    post.assert_not_awaited()


async def test_deliver_allows_dns_names_resolving_to_public_addresses() -> None:
    manager = _make_manager(("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"))
    cfg = WebhookConfig(
        webhook_id="wh1",
        session_id="s1",
        url="https://webhook.example/hook",
        event_types=None,
        pattern=None,
        secret=None,
    )
    resp = MagicMock()
    resp.is_success = True

    post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.post", new=post):
        await manager._deliver(cfg, _make_event())

    post.assert_awaited_once()


# ---------------------------------------------------------------------------
# event_types filter
# ---------------------------------------------------------------------------


async def test_event_types_filter_drops_unmatched() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    received_types: list[str] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", b"")
        payload = json.loads(body)
        received_types.append(payload["event"]["type"])
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/hook", event_types=["hijack_acquired"], event_bus=bus)
        await asyncio.sleep(0.05)
        # snapshot should be filtered
        await hub.append_event("s1", "snapshot", {"screen": "$ x"})
        await asyncio.sleep(0.1)
        # hijack_acquired should pass
        await hub.append_event("s1", "hijack_acquired", {"hijack_id": "abc"})
        await asyncio.sleep(0.2)

    assert received_types == ["hijack_acquired"]
    await manager.shutdown()


# ---------------------------------------------------------------------------
# pattern filter
# ---------------------------------------------------------------------------


async def test_pattern_filter_drops_non_matching() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    received_screens: list[str] = []

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", b"")
        payload = json.loads(body)
        screen = payload["event"].get("data", {}).get("screen", "")
        received_screens.append(screen)
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register(
            "s1",
            "https://example.com/hook",
            event_types=["snapshot"],
            pattern=r"\$ ",
            event_bus=bus,
        )
        await asyncio.sleep(0.05)
        # non-matching — filtered by EventBus.watch pattern
        await hub.append_event("s1", "snapshot", {"screen": "loading..."})
        await asyncio.sleep(0.1)
        # matching
        await hub.append_event("s1", "snapshot", {"screen": "root@host:~$ "})
        await asyncio.sleep(0.2)

    assert received_screens == ["root@host:~$ "]
    await manager.shutdown()


# ---------------------------------------------------------------------------
# worker disconnect sentinel stops delivery loop
# ---------------------------------------------------------------------------


async def test_delivery_loop_stops_on_worker_disconnect() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    call_count = 0

    async def _mock_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        cfg = await manager.register("s1", "https://example.com/hook", event_bus=bus)
        await asyncio.sleep(0.05)
        bus.close_worker("s1")
        # Wait for task to finish
        task = manager._tasks[cfg.webhook_id]
        await asyncio.wait_for(task, timeout=2.0)

    assert task.done()
    assert call_count == 0  # no events, just sentinel
    await manager.shutdown()


# ---------------------------------------------------------------------------
# shutdown cancels running tasks
# ---------------------------------------------------------------------------


async def test_shutdown_cancels_delivery_tasks() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    cfg = await manager.register("s1", "https://example.com/hook", event_bus=bus)
    task = manager._tasks[cfg.webhook_id]
    assert not task.done()

    await manager.shutdown()

    assert task.done()


# ---------------------------------------------------------------------------
# Multiple webhooks for same session
# ---------------------------------------------------------------------------


async def test_multiple_webhooks_both_receive_events() -> None:
    bus, hub = await _make_bus_with_worker("s1")
    manager = _make_manager()

    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []

    urls_seen: list[str] = []

    async def _mock_post(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        body = kwargs.get("content", b"")
        payload = json.loads(body)
        urls_seen.append(url)
        if url == "https://example.com/a":
            received_a.append(payload)
        else:
            received_b.append(payload)
        resp = MagicMock()
        resp.is_success = True
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_post)):
        await manager.register("s1", "https://example.com/a", event_bus=bus)
        await manager.register("s1", "https://example.com/b", event_bus=bus)
        await asyncio.sleep(0.05)
        await hub.append_event("s1", "snapshot", {"screen": "$ both"})
        await asyncio.sleep(0.3)

    assert len(received_a) >= 1
    assert len(received_b) >= 1
    await manager.shutdown()


# ---------------------------------------------------------------------------
# SSRF guard — DNS resolution at registration time
# ---------------------------------------------------------------------------


def test_validate_webhook_url_rejects_dns_name_resolving_to_metadata_ip() -> None:
    """A hostname that DNS-resolves to a cloud-metadata IP must be rejected."""
    from provide.uterm.server import webhooks as wh

    with patch.object(wh, "_resolve_hostname_sync", return_value=("169.254.169.254",)):
        with pytest.raises(ValueError, match="not allowed"):
            wh.validate_webhook_url("https://malicious.example.com/hook")
