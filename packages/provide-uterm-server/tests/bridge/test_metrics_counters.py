#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for new observability counters: rate-limit drops, webhook failures, event-bus drops.

Commit 2 of the P1-1 observability hardening.  Each test triggers the relevant
event and asserts the counter increments via the on_metric callback or via
hub.metric (which forwards to the shared metrics dict).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.bridge.hub.event_bus import _Subscription
from provide.uterm.server.bridge.ratelimit import TokenBucket
from provide.uterm.server.webhooks import WebhookManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metrics() -> dict[str, int]:
    return {}


def _make_hub_with_metrics(
    metrics: dict[str, int] | None = None,
    **hub_kwargs: Any,
) -> tuple[TermHub, dict[str, int]]:
    if metrics is None:
        metrics = _make_metrics()

    def on_metric(name: str, value: int = 1) -> None:
        metrics[name] = metrics.get(name, 0) + value

    hub_kwargs.setdefault("resolve_browser_role", lambda _ws, _wid: "operator")
    hub = TermHub(on_metric=on_metric, **hub_kwargs)
    return hub, metrics


def _make_app_with_hub(hub: TermHub) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return app, client


# ---------------------------------------------------------------------------
# REST acquire rate-limit counter
# ---------------------------------------------------------------------------


class TestRestAcquireRateLimitedCounter:
    def test_rest_acquire_rate_limited_total_increments(self) -> None:
        """rest_acquire_rate_limited_total increments when allow_rest_acquire_for returns False."""
        hub, metrics = _make_hub_with_metrics()
        _app, client = _make_app_with_hub(hub)

        with patch.object(hub, "allow_rest_acquire_for", return_value=False):
            resp = client.post("/worker/w1/hijack/acquire", json={})
        assert resp.status_code == 429
        assert metrics.get("rest_acquire_rate_limited_total", 0) == 1


# ---------------------------------------------------------------------------
# REST send rate-limit counter
# ---------------------------------------------------------------------------


class TestRestSendRateLimitedCounter:
    def test_rest_send_rate_limited_total_increments(self) -> None:
        """rest_send_rate_limited_total increments when allow_rest_send_for returns False on send."""
        hub, metrics = _make_hub_with_metrics()
        _app, client = _make_app_with_hub(hub)

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(
                "/worker/w1/hijack/abcdef12-0000-0000-0000-000000000000/send",
                json={"keys": "hello"},
            )
        assert resp.status_code == 429
        assert metrics.get("rest_send_rate_limited_total", 0) == 1


# ---------------------------------------------------------------------------
# REST step rate-limit counter
# ---------------------------------------------------------------------------


class TestRestStepRateLimitedCounter:
    def test_rest_step_rate_limited_total_increments(self) -> None:
        """rest_step_rate_limited_total increments when allow_rest_send_for returns False on step."""
        hub, metrics = _make_hub_with_metrics()
        _app, client = _make_app_with_hub(hub)

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(
                "/worker/w1/hijack/abcdef12-0000-0000-0000-000000000000/step",
            )
        assert resp.status_code == 429
        assert metrics.get("rest_step_rate_limited_total", 0) == 1


# ---------------------------------------------------------------------------
# WebSocket browser rate-limit counters
# ---------------------------------------------------------------------------


class TestWsBrowserRateLimitedCounters:
    def test_ws_browser_rate_limited_total_increments(self) -> None:
        """ws_browser_rate_limited_total increments when browser input bucket exhausted."""
        # Use a very low rate limit (burst=1) so the 2nd and 3rd input frames get dropped.
        metrics2: dict[str, int] = {}

        def on_metric(name: str, value: int = 1) -> None:
            metrics2[name] = metrics2.get(name, 0) + value

        hub2 = TermHub(
            on_metric=on_metric,
            browser_rate_limit_per_sec=1,
            resolve_browser_role=lambda _ws, _wid: "operator",
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())

        with TestClient(app2) as client:
            with connect_test_ws(client, "/ws/worker/w1/term") as worker_ws:
                worker_ws.receive_json()  # snapshot_req
                # Enable open input mode so browser can send input
                client.post("/worker/w1/input_mode", json={"input_mode": "open"})

                with connect_test_ws(client, "/ws/browser/w1/term") as browser_ws:
                    browser_ws.receive_json()  # hello
                    browser_ws.receive_json()  # hijack_state

                    # Send 3 input frames — burst=1, so 2nd and 3rd are rate-limited
                    browser_ws.send_json({"type": "input", "data": "a"})
                    browser_ws.send_json({"type": "input", "data": "b"})
                    browser_ws.send_json({"type": "input", "data": "c"})

        assert metrics2.get("ws_browser_rate_limited_total", 0) >= 1

    def test_ws_browser_control_rate_limited_total_increments(self) -> None:
        """ws_browser_control_rate_limited_total increments when browser control bucket exhausted."""
        metrics2: dict[str, int] = {}

        def on_metric(name: str, value: int = 1) -> None:
            metrics2[name] = metrics2.get(name, 0) + value

        hub2 = TermHub(
            on_metric=on_metric,
            # Allow input; deny control by setting control limit to 1 burst
            browser_rate_limit_per_sec=10000,
            resolve_browser_role=lambda _ws, _wid: "operator",
        )
        app2 = FastAPI()
        app2.include_router(hub2.create_router())

        # Patch TokenBucket.allow to deny the control bucket specifically.
        # The browser loop creates _browser_bucket then _browser_control_bucket.
        # We intercept by patching allow() on all TokenBucket instances via the class.
        call_count = [0]

        def patched_allow(self: TokenBucket) -> bool:
            call_count[0] += 1
            # First call per message is the input bucket check — let that pass.
            # Subsequent calls (control bucket) are denied.
            return call_count[0] % 2 == 1

        with TestClient(app2) as client:
            with connect_test_ws(client, "/ws/worker/w1/term") as worker_ws:
                worker_ws.receive_json()  # snapshot_req
                client.post("/worker/w1/input_mode", json={"input_mode": "open"})

                with connect_test_ws(client, "/ws/browser/w1/term") as browser_ws:
                    browser_ws.receive_json()  # hello
                    browser_ws.receive_json()  # hijack_state

                    with patch.object(TokenBucket, "allow", patched_allow):
                        # Send a non-input control frame (hijack_request)
                        browser_ws.send_json({"type": "hijack_request"})
                        browser_ws.send_json({"type": "hijack_request"})
                        browser_ws.send_json({"type": "hijack_request"})

        assert metrics2.get("ws_browser_control_rate_limited_total", 0) >= 1


# ---------------------------------------------------------------------------
# WebhookManager on_metric callback
# ---------------------------------------------------------------------------


class TestWebhookManagerOnMetric:
    def test_on_metric_defaults_to_none(self) -> None:
        """WebhookManager can be constructed without on_metric — existing tests pass."""
        mgr = WebhookManager(resolver=lambda _h: ("1.2.3.4",))
        # No error; on_metric is optional.
        assert mgr is not None

    async def test_webhook_delivery_blocked_total_increments(self) -> None:
        """webhook_delivery_blocked_total fires when SSRF guard blocks delivery."""
        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        mgr = WebhookManager(
            resolver=lambda _h: ("169.254.169.254",),  # metadata IP → blocked
            on_metric=on_metric,
        )
        cfg = await mgr.register("s1", "https://example.com/hook")

        # Deliver directly; _deliver_url_allowed resolves in the async path
        from provide.uterm.server.webhooks import WebhookConfig

        cfg_blocked = WebhookConfig(
            webhook_id=cfg.webhook_id,
            session_id=cfg.session_id,
            url="https://blocked-host.example/hook",
            event_types=None,
            pattern=None,
            secret=None,
        )
        # Patch _delivery_url_allowed to return False synchronously
        with patch("provide.uterm.server.webhooks._delivery_url_allowed", AsyncMock(return_value=False)):
            await mgr._deliver(cfg_blocked, {"type": "snapshot"})

        await mgr.shutdown()
        counter_calls = [c for c in calls if c[0] == "webhook_delivery_blocked_total"]
        assert counter_calls, f"expected webhook_delivery_blocked_total in {calls}"

    async def test_webhook_delivery_failed_total_increments(self) -> None:
        """webhook_delivery_failed_total fires on non-2xx HTTP response."""
        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        mgr = WebhookManager(
            resolver=lambda _h: ("93.184.216.34",),
            on_metric=on_metric,
        )
        cfg = await mgr.register("s1", "https://example.com/hook")

        import httpx
        import respx

        with respx.mock:
            respx.post("https://example.com/hook").mock(return_value=httpx.Response(500))
            # Only one attempt by limiting retries — deliver will fail then give up
            with patch("provide.uterm.server.webhooks._RETRY_DELAYS", ()):
                await mgr._deliver(cfg, {"type": "snapshot"})

        await mgr.shutdown()
        failed_calls = [c for c in calls if c[0] == "webhook_delivery_failed_total"]
        assert failed_calls, f"expected webhook_delivery_failed_total in {calls}"

    async def test_webhook_delivery_giving_up_total_increments(self) -> None:
        """webhook_delivery_giving_up_total fires when all retry attempts exhausted."""
        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        mgr = WebhookManager(
            resolver=lambda _h: ("93.184.216.34",),
            on_metric=on_metric,
        )
        cfg = await mgr.register("s1", "https://example.com/hook")

        import httpx
        import respx

        with respx.mock:
            respx.post("https://example.com/hook").mock(return_value=httpx.Response(500))
            with patch("provide.uterm.server.webhooks._RETRY_DELAYS", ()):
                await mgr._deliver(cfg, {"type": "snapshot"})

        await mgr.shutdown()
        giving_up_calls = [c for c in calls if c[0] == "webhook_delivery_giving_up_total"]
        assert giving_up_calls, f"expected webhook_delivery_giving_up_total in {calls}"

    async def test_webhook_auto_unregistered_total_increments(self) -> None:
        """webhook_auto_unregistered_total fires when block threshold exceeded."""
        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        mgr = WebhookManager(
            resolver=lambda _h: ("169.254.169.254",),
            on_metric=on_metric,
        )
        cfg = await mgr.register("s1", "https://example.com/hook")

        from provide.uterm.server.webhooks import _MAX_BLOCKED_DELIVERIES

        with patch("provide.uterm.server.webhooks._delivery_url_allowed", AsyncMock(return_value=False)):
            for _ in range(_MAX_BLOCKED_DELIVERIES):
                await mgr._deliver(cfg, {"type": "snapshot"})
            # Allow background unregister task to complete
            import asyncio

            await asyncio.sleep(0.05)

        await mgr.shutdown()
        unreg_calls = [c for c in calls if c[0] == "webhook_auto_unregistered_total"]
        assert unreg_calls, f"expected webhook_auto_unregistered_total in {calls}"


# ---------------------------------------------------------------------------
# EventBus on_metric callback
# ---------------------------------------------------------------------------


class TestEventBusOnMetric:
    def test_on_metric_defaults_to_none(self) -> None:
        """EventBus can be constructed without on_metric — existing tests pass."""
        bus = EventBus()
        assert bus is not None

    def test_event_bus_subscriber_drop_total_increments_on_queue_full(self) -> None:
        """event_bus_subscriber_drop_total fires when _deliver drops a full queue."""
        import asyncio

        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        bus = EventBus(max_queue_depth=1, on_metric=on_metric)
        sub = _Subscription(
            sub_id="s1",
            worker_id="w1",
            queue=asyncio.Queue(maxsize=1),
            event_types=None,
            pattern=None,
        )
        # Fill the queue so the next _deliver triggers a drop
        sub.queue.put_nowait({"type": "snapshot"})
        bus._deliver(sub, "w1", {"type": "snapshot"})
        drop_calls = [c for c in calls if c[0] == "event_bus_subscriber_drop_total"]
        assert drop_calls, f"expected event_bus_subscriber_drop_total in {calls}"

    def test_event_bus_subscriber_drop_total_increments_on_sentinel_full(self) -> None:
        """event_bus_subscriber_drop_total fires when _put_sentinel drops on full queue."""
        import asyncio

        calls: list[tuple[str, int]] = []

        def on_metric(name: str, value: int = 1) -> None:
            calls.append((name, value))

        bus = EventBus(max_queue_depth=1, on_metric=on_metric)
        sub = _Subscription(
            sub_id="s1",
            worker_id="w1",
            queue=asyncio.Queue(maxsize=1),
            event_types=None,
            pattern=None,
        )
        # Fill the queue
        sub.queue.put_nowait({"type": "snapshot"})
        # _put_sentinel with a full queue should trigger a drop + sentinel
        bus._put_sentinel(sub)
        drop_calls = [c for c in calls if c[0] == "event_bus_subscriber_drop_total"]
        assert drop_calls, f"expected event_bus_subscriber_drop_total in {calls}"


# ---------------------------------------------------------------------------
# Pre-seeded counters in factory metrics dict
# ---------------------------------------------------------------------------


class TestPreSeededCounters:
    def test_new_counters_preseeded_in_metrics(self) -> None:
        """All 10 new counters must be pre-seeded at 0 in the metrics dict."""
        from provide.uterm.server import create_server_app, default_server_config

        config = default_server_config()
        config.auth.mode = "header"
        config.auth.header_mode_acknowledged = True
        config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        app = create_server_app(config)
        metrics: dict[str, int] = app.state.uterm_metrics  # type: ignore[assignment]

        expected = [
            "ws_browser_rate_limited_total",
            "ws_browser_control_rate_limited_total",
            "rest_acquire_rate_limited_total",
            "rest_send_rate_limited_total",
            "rest_step_rate_limited_total",
            "webhook_delivery_blocked_total",
            "webhook_auto_unregistered_total",
            "webhook_delivery_failed_total",
            "webhook_delivery_giving_up_total",
            "event_bus_subscriber_drop_total",
        ]
        missing = [name for name in expected if name not in metrics]
        assert not missing, f"These counters not pre-seeded: {missing}"
        # All start at 0
        for name in expected:
            assert metrics[name] == 0, f"{name} should start at 0, got {metrics[name]}"
