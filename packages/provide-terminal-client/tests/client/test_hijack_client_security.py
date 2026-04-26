#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from provide.terminal.client.hijack import HijackClient


@pytest.mark.asyncio
async def test_request_sanitization_on_http_error():
    with patch("provide.terminal.client.hijack.log") as mock_log:
        client = HijackClient("http://test")

        # Mock a response with sensitive data
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.is_success = False
        sensitive_data = {"error": "Forbidden", "token": "secret-123", "secret": "shh", "password": "password123"}
        mock_response.json.return_value = sensitive_data
        mock_response.text = str(sensitive_data)

        # Raise HTTPStatusError (which is an HTTPError)
        exc = httpx.HTTPStatusError("Forbidden", request=MagicMock(), response=mock_response)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_httpx_client = MagicMock(spec=httpx.AsyncClient)
            mock_httpx_client.request = AsyncMock(side_effect=exc)
            mock_get_client.return_value = mock_httpx_client

            ok, body = await client._request("GET", "/test")
            assert ok is False

            # Verify logging happened and was sanitized
            found_warning = False
            for call in mock_log.warning.call_args_list:
                found_warning = True
                log_args = str(call.args)
                assert "secret-123" not in log_args
                assert "shh" not in log_args
                assert "password123" not in log_args
            assert found_warning, "Expected log.warning to be called"


@pytest.mark.asyncio
async def test_request_sanitization_on_failure_response():
    with patch("provide.terminal.client.hijack.log") as mock_log:
        client = HijackClient("http://test")

        # Mock a response with sensitive data, but no exception raised by httpx
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_response.is_success = False
        sensitive_data = {"error": "Unauthorized", "token": "hidden-token"}
        mock_response.json.return_value = sensitive_data
        mock_response.text = str(sensitive_data)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_httpx_client = MagicMock(spec=httpx.AsyncClient)
            mock_httpx_client.request = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_httpx_client

            ok, body = await client._request("GET", "/test")
            assert ok is False

            # Verify logging happened and was sanitized
            found_warning = False
            for call in mock_log.warning.call_args_list:
                found_warning = True
                log_args = str(call.args)
                assert "hidden-token" not in log_args
            assert found_warning, "Expected log.warning to be called on failure response"
