#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic ownership-fencing races for the Cloudflare Durable Object."""

from __future__ import annotations

import asyncio

import pytest
from cf_fencing_helpers import (
    _BlockingWorkerWs,
    _BrowserWs,
    _FailingBrowserWs,
    _release,
    _Request,
    _runtime,
    _send,
)
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.api.http_routes._hijack import route_hijack
from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
from provide.uterm.cloudflare.contracts import frame_json


@pytest.mark.parametrize("source", ["rest_send", "rest_step", "browser"])
async def test_release_waits_for_owned_delivery(source: str) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    if source == "rest_send":
        delivery = _send(runtime, hijack_id)
    elif source == "rest_step":
        delivery = route_hijack(
            runtime,
            _Request(),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/step",
            "https://example.invalid/step",
            "POST",
        )
    else:
        browser = _BrowserWs()
        runtime.browser_hijack_owner[runtime.ws_key(browser)] = hijack_id
        delivery = handle_socket_message(runtime, browser, frame_json("input", data="owned-input"), is_worker=False)

    delivery_task = asyncio.create_task(delivery)
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    release_task = asyncio.create_task(_release(runtime, hijack_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not release_task.done(), "release crossed an in-flight owned delivery"
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(release_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_alarm_expiry_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(_send(runtime, hijack_id))
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    assert runtime.hijack._session is not None
    runtime.hijack._session.lease_expires_at = 0
    expired_raw_session = runtime.hijack._session
    assert runtime.hijack.session is None
    assert runtime.hijack._session is expired_raw_session

    alarm_task = asyncio.create_task(runtime.alarm())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not alarm_task.done(), "lease expiry crossed an in-flight owned delivery"

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(alarm_task, timeout=1)
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_worker_replacement_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    old_worker = _BlockingWorkerWs()
    runtime.worker_ws = old_worker

    delivery_task = asyncio.create_task(_send(runtime, acquired.session.hijack_id))
    await asyncio.wait_for(old_worker.started.wait(), timeout=1)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    replacement_task = asyncio.create_task(runtime.register_worker_socket(new_worker))
    await asyncio.sleep(0)

    assert not replacement_task.done()
    assert runtime.worker_ws is old_worker

    old_worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(replacement_task, timeout=1)
    assert runtime.worker_ws is new_worker


async def test_worker_disconnect_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    assert await runtime.register_worker_socket(worker)
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None

    delivery_task = asyncio.create_task(_send(runtime, acquired.session.hijack_id))
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    disconnect_task = asyncio.create_task(runtime.unregister_worker_socket(worker))
    await asyncio.sleep(0)

    assert not disconnect_task.done()
    assert runtime.worker_ws is worker

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    assert await asyncio.wait_for(disconnect_task, timeout=1)
    assert runtime.worker_ws is None


async def test_worker_send_timeout_is_bounded_and_fails_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker
    monkeypatch.setattr(
        "provide.uterm.cloudflare.do.session_runtime.io._WORKER_SEND_TIMEOUT_S",
        0.01,
    )

    response = await asyncio.wait_for(
        _send(runtime, acquired.session.hijack_id),
        timeout=0.5,
    )

    assert getattr(response, "status", None) == 409
    assert runtime.worker_ws is None


async def test_failed_browser_broadcast_removal_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    owner = _FailingBrowserWs()
    owner_key = runtime.ws_key(owner)
    runtime.browser_sockets[owner_key] = owner
    runtime.store.create_resume_token("failed-browser-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[owner_key] = "failed-browser-token"
    acquired = runtime.hijack.acquire("browser:failed-browser-token", 60)
    assert acquired.session is not None
    runtime.browser_hijack_owner[owner_key] = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(_send(runtime, acquired.session.hijack_id))
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    failed_broadcast = asyncio.create_task(runtime.broadcast_hijack_state())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not failed_broadcast.done()
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(failed_broadcast, timeout=1)
    assert runtime.hijack.session is None
    record = runtime.store.get_resume_token("failed-browser-token")
    assert record is not None and record["was_hijack_owner"] is True


async def test_session_delete_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(_send(runtime, acquired.session.hijack_id))
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    delete_task = asyncio.create_task(
        route_http(
            runtime,
            _Request(
                url=f"https://example.invalid/api/sessions/{runtime.worker_id}",
                method="DELETE",
            ),
        )
    )
    await asyncio.sleep(0)

    assert not delete_task.done()
    assert runtime._deleted_at is None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(delete_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.lifecycle_state == "deleted"
    assert runtime.worker_ws is None
    assert runtime.hijack.session is None
