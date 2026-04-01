#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared types for tunnel token state and API responses.

Used by both FastAPI server and Cloudflare Worker implementations
to maintain a consistent token contract across deployment surfaces.
"""

from __future__ import annotations

from typing import TypedDict


class TunnelTokenState(TypedDict):
    """In-memory / KV token state for a tunnel session.

    SECURITY: These fields are secret credentials. Do not log token values.
    Log only token_type identifiers and validation results.
    """

    worker_token: str
    share_token: str
    control_token: str
    created_at: float
    expires_at: float
    issued_ip: str | None
    tunnel_type: str


class TunnelCreateResponse(TypedDict):
    """Shape of the POST /api/tunnels response body."""

    tunnel_id: str
    display_name: str
    tunnel_type: str
    ws_endpoint: str
    worker_token: str
    share_url: str
    control_url: str
    expires_at: float


class HttpRequestMessage(TypedDict, total=False):
    """Structured HTTP request sent on channel 0x03."""

    type: str  # "http_req"
    id: str
    ts: float
    method: str
    url: str
    headers: dict[str, str]
    body_size: int
    body_b64: str
    body_truncated: bool
    body_binary: bool


class HttpResponseMessage(TypedDict, total=False):
    """Structured HTTP response sent on channel 0x03."""

    type: str  # "http_res"
    id: str
    ts: float
    status: int
    status_text: str
    headers: dict[str, str]
    body_size: int
    body_b64: str
    body_truncated: bool
    body_binary: bool
    duration_ms: float
