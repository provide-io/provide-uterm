#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import json

import httpx2
import pytest

from provide.uterm.server.recording import WebhookRecordingStore
from tests.helpers import http_mock


@pytest.mark.asyncio
async def test_webhook_recording_append_events():
    url = "https://fleet.example.com/webhooks/recording"
    secret = "uterm-test-secret-32-byte-minimum-key"
    store = WebhookRecordingStore(url, secret=secret)

    session_id = "test-session"
    events = [{"ts": 1234567890, "event": "data", "data": "hello"}]

    async with http_mock.mock:
        route = http_mock.post(url).mock(return_value=httpx2.Response(200))

        await store.append_events(session_id, events)

        assert route.called
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {secret}"

        data = json.loads(request.read().decode())
        assert data["session_id"] == session_id
        assert data["action"] == "append"
        assert data["events"] == events


@pytest.mark.asyncio
async def test_webhook_recording_get_entries():
    url = "https://fleet.example.com/webhooks/recording"
    secret = "uterm-test-secret-32-byte-minimum-key"
    store = WebhookRecordingStore(url, secret=secret)

    session_id = "test-session"
    mock_entries = [{"ts": 123, "event": "data", "data": "x"}]

    async with http_mock.mock:
        # WebhookRecordingStore._get uses f"{self.url}/{session_id}/{action}"
        expected_url = f"{url}/{session_id}/entries"
        route = http_mock.get(expected_url).mock(return_value=httpx2.Response(200, json={"entries": mock_entries}))

        entries = await store.get_entries(session_id, limit=10, offset=5, event="data")

        assert route.called
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {secret}"
        # httpx2.QueryParams are used, let's check them
        assert request.url.params["limit"] == "10"
        assert request.url.params["offset"] == "5"
        assert request.url.params["event"] == "data"
        assert entries == mock_entries


@pytest.mark.asyncio
async def test_webhook_recording_start_session():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    session_id = "test-session"
    metadata = {"user": "alice", "rows": 24, "cols": 80}

    async with http_mock.mock:
        route = http_mock.post(url).mock(return_value=httpx2.Response(200))

        await store.start_session(session_id, metadata)

        assert route.called
        data = json.loads(route.calls.last.request.read().decode())
        assert data["session_id"] == session_id
        assert data["action"] == "start"
        assert data["metadata"] == metadata


@pytest.mark.asyncio
async def test_webhook_recording_end_session():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    session_id = "test-session"

    async with http_mock.mock:
        route = http_mock.post(url).mock(return_value=httpx2.Response(200))

        await store.end_session(session_id)

        assert route.called
        data = json.loads(route.calls.last.request.read().decode())
        assert data["session_id"] == session_id
        assert data["action"] == "end"


@pytest.mark.asyncio
async def test_webhook_recording_meta():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    session_id = "test-session"
    mock_meta = {"session_id": session_id, "exists": True, "size_bytes": 100}

    async with http_mock.mock:
        expected_url = f"{url}/{session_id}/meta"
        route = http_mock.get(expected_url).mock(return_value=httpx2.Response(200, json=mock_meta))

        meta = await store.recording_meta(session_id)

        assert route.called
        assert meta == mock_meta


@pytest.mark.asyncio
async def test_webhook_recording_meta_not_found():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    session_id = "missing"

    async with http_mock.mock:
        expected_url = f"{url}/{session_id}/meta"
        http_mock.get(expected_url).mock(return_value=httpx2.Response(404))

        meta = await store.recording_meta(session_id)

        assert meta == {"session_id": session_id, "exists": False, "size_bytes": 0}


@pytest.mark.asyncio
async def test_webhook_recording_get_path():
    store = WebhookRecordingStore("http://url")
    assert await store.get_path("sid") is None


@pytest.mark.asyncio
async def test_webhook_recording_get_entries_invalid_response():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)
    async with http_mock.mock:
        http_mock.get(f"{url}/sid/entries").mock(return_value=httpx2.Response(200, json=["not", "a", "dict"]))
        entries = await store.get_entries("sid")
        assert entries == []


@pytest.mark.asyncio
async def test_webhook_recording_get_failure_exception():
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)
    async with http_mock.mock:
        http_mock.get(f"{url}/sid/meta").mock(side_effect=httpx2.ConnectError("fail"))
        meta = await store.recording_meta("sid")
        assert meta == {"session_id": "sid", "exists": False, "size_bytes": 0}


@pytest.mark.asyncio
async def test_webhook_recording_post_failure_best_effort():
    # Verify that post failures (like 500) are handled gracefully (though they don't trigger except Exception)
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        http_mock.post(url).mock(return_value=httpx2.Response(500))
        await store.append_events("sid", [])


@pytest.mark.asyncio
async def test_webhook_recording_post_failure_exception():
    # Verify that post exceptions are caught
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        http_mock.post(url).mock(side_effect=httpx2.ConnectError("fail"))
        await store.append_events("sid", [])


# ---------------------------------------------------------------------------
# L28: outbound recording webhooks honour the egress SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_recording_post_metadata_url_not_sent():
    """_post to a cloud-metadata IP must be blocked by the egress guard and
    degrade gracefully (no HTTP request made, no exception raised)."""
    url = "http://169.254.169.254/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        route = http_mock.post(url).mock(return_value=httpx2.Response(200))
        # start_session → _post; must not raise and must not POST.
        await store.start_session("sid", {"user": "alice"})
        assert not route.called


@pytest.mark.asyncio
async def test_webhook_recording_get_metadata_url_not_sent():
    """_get to a cloud-metadata IP must be blocked by the egress guard and
    degrade gracefully (no HTTP request made, meta returns the not-found default)."""
    url = "http://169.254.169.254/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        route = http_mock.get(f"{url}/sid/meta").mock(return_value=httpx2.Response(200, json={"x": 1}))
        meta = await store.recording_meta("sid")
        assert not route.called
        # _get returned None (guard blocked) → recording_meta falls back to default.
        assert meta == {"session_id": "sid", "exists": False, "size_bytes": 0}


@pytest.mark.asyncio
async def test_webhook_recording_post_allowed_url_proceeds():
    """With an allowed (benign-resolving) URL the POST proceeds normally.

    The autouse _stub_egress_resolver fixture resolves the host to a benign
    public IP, so the egress guard passes and the request is sent.
    """
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        route = http_mock.post(url).mock(return_value=httpx2.Response(200))
        await store.start_session("sid", {"user": "alice"})
        assert route.called


@pytest.mark.asyncio
async def test_webhook_recording_get_allowed_url_proceeds():
    """With an allowed URL the _get request proceeds normally."""
    url = "https://fleet.example.com/webhooks/recording"
    store = WebhookRecordingStore(url)

    async with http_mock.mock:
        route = http_mock.get(f"{url}/sid/meta").mock(return_value=httpx2.Response(200, json={"exists": True}))
        meta = await store.recording_meta("sid")
        assert route.called
        assert meta == {"exists": True}
