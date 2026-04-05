#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for CF tunnel HTTP channel handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from provide.terminal.cloudflare.api.tunnel_routes import handle_tunnel_message


class _MockRuntime:
    def __init__(self):
        self.worker_id = "tunnel-http-test"
        self.lifecycle_state = "running"
        self.last_snapshot = None
        self.broadcast_worker_frame = AsyncMock()


class TestCfHttpChannel:
    @pytest.mark.asyncio
    async def test_http_channel_broadcasts(self):
        rt = _MockRuntime()
        payload = json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/api"}).encode()
        frame = b"\x03\x00" + payload  # channel 3, flags 0
        await handle_tunnel_message(rt, MagicMock(), frame)
        rt.broadcast_worker_frame.assert_called_once()
        msg = rt.broadcast_worker_frame.call_args[0][0]
        assert msg["type"] == "http_req"
        assert msg["_channel"] == "http"

    @pytest.mark.asyncio
    async def test_http_channel_invalid_json(self):
        rt = _MockRuntime()
        frame = b"\x03\x00not json"
        await handle_tunnel_message(rt, MagicMock(), frame)
        rt.broadcast_worker_frame.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_channel_does_not_update_snapshot(self):
        rt = _MockRuntime()
        payload = json.dumps({"type": "http_res", "id": "r1", "status": 200}).encode()
        frame = b"\x03\x00" + payload
        await handle_tunnel_message(rt, MagicMock(), frame)
        assert rt.last_snapshot is None

    @pytest.mark.asyncio
    async def test_control_error_message(self):
        """Line 113-114: 'error' control type handled."""
        rt = _MockRuntime()
        ctrl = json.dumps({"type": "error", "message": "upstream timeout"}).encode()
        frame = b"\x00\x00" + ctrl  # channel 0 = control
        await handle_tunnel_message(rt, MagicMock(), frame)

    @pytest.mark.asyncio
    async def test_control_unknown_type(self):
        """Line 115-116: unknown control type handled."""
        rt = _MockRuntime()
        ctrl = json.dumps({"type": "custom_extension", "data": 42}).encode()
        frame = b"\x00\x00" + ctrl
        await handle_tunnel_message(rt, MagicMock(), frame)
