#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Async REST client for the provide-terminal hijack control plane.

Wraps :mod:`httpx.AsyncClient` to provide typed methods for every hijack
and session endpoint.  Returns ``tuple[bool, dict]`` from each call —
matching the agent ``_manager_request()`` convention for zero-effort
migration.

Usage::

    async with HijackClient("http://localhost:8780") as c:
        ok, data = await c.acquire("worker-1", owner="agent")
        if ok:
            ok, snap = await c.snapshot("worker-1", data["hijack_id"])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from provide.telemetry import get_logger

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)


def _sanitize(data: Any) -> Any:
    """Deeply strip sensitive keys and truncate long strings/lists."""
    if isinstance(data, dict):
        return {
            k: ("***" if k.lower() in ("token", "secret", "password", "key", "auth", "session_id") else _sanitize(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        if len(data) > 10:
            return [_sanitize(x) for x in data[:10]] + ["..."]
        return [_sanitize(x) for x in data]
    if isinstance(data, str) and len(data) > 500:
        return f"{data[:500]}..."
    return data


class HijackClient:
    """Async REST client for the provide-terminal hijack + session API.

    Parameters
    ----------
    base_url:
        Root URL of the provide-terminal server (e.g. ``http://localhost:8780``).
    entity_prefix:
        Path prefix for worker endpoints.  ``"/worker"`` for provide-terminal,
        ``"/agent"`` for agent compatibility.
    timeout:
        Default request timeout in seconds.
    headers:
        Extra headers sent with every request (e.g. auth tokens).
    """

    def __init__(
        self,
        base_url: str,
        *,
        entity_prefix: str = "/worker",
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._entity_prefix = entity_prefix.rstrip("/")
        self._timeout = timeout
        self._extra_headers = headers or {}
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._owns_client = True

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> HijackClient:
        kw: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._extra_headers,
            "timeout": httpx.Timeout(self._timeout),
        }
        if self._transport is not None:
            kw["transport"] = self._transport
        self._client = httpx.AsyncClient(**kw)
        self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- internal transport ---------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # Lazy single-request client (caller did not use ``async with``).
        kw: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._extra_headers,
            "timeout": httpx.Timeout(self._timeout),
        }
        if self._transport is not None:
            kw["transport"] = self._transport
        self._client = httpx.AsyncClient(**kw)
        self._owns_client = True
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[bool, Any]:
        """Issue an HTTP request and return ``(ok, body_or_error)``."""
        client = self._get_client()
        kw: dict[str, Any] = {"json": json, "params": params}
        if timeout is not None:
            kw["timeout"] = httpx.Timeout(timeout)
        try:
            r = await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            msg = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    body = exc.response.json()
                    msg += f" - body: {_sanitize(body)}"
                except Exception:
                    msg += f" - body: {_sanitize(exc.response.text)}"
            log.warning("HijackClient %s %s failed: %s", method, path, msg)
            return False, {"error": str(exc)}

        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}

        if r.is_success:
            return True, body

        log.warning(
            "HijackClient %s %s failed (status %d): %s",
            method,
            path,
            r.status_code,
            _sanitize(body),
        )
        return False, body

    # -- worker path helpers --------------------------------------------------

    def _wp(self, worker_id: str) -> str:
        return f"{self._entity_prefix}/{worker_id}"

    def _hp(self, worker_id: str, hijack_id: str) -> str:
        return f"{self._entity_prefix}/{worker_id}/hijack/{hijack_id}"

    # -- hijack lifecycle -----------------------------------------------------

    async def acquire(
        self,
        worker_id: str,
        *,
        owner: str = "operator",
        lease_s: int = 90,
    ) -> tuple[bool, dict[str, Any]]:
        """Acquire a lease-based hijack session."""
        return await self._request(
            "POST",
            f"{self._wp(worker_id)}/hijack/acquire",
            json={"owner": owner, "lease_s": lease_s},
        )

    async def heartbeat(
        self,
        worker_id: str,
        hijack_id: str,
        *,
        lease_s: int = 90,
    ) -> tuple[bool, dict[str, Any]]:
        """Extend a hijack lease."""
        return await self._request(
            "POST",
            f"{self._hp(worker_id, hijack_id)}/heartbeat",
            json={"lease_s": lease_s},
        )

    async def send(
        self,
        worker_id: str,
        hijack_id: str,
        *,
        keys: str,
        expect_prompt_id: str | None = None,
        expect_regex: str | None = None,
        timeout_ms: int = 2000,
        poll_interval_ms: int = 120,
    ) -> tuple[bool, dict[str, Any]]:
        """Send input to a hijacked worker."""
        body: dict[str, Any] = {
            "keys": keys,
            "timeout_ms": timeout_ms,
            "poll_interval_ms": poll_interval_ms,
        }
        if expect_prompt_id is not None:
            body["expect_prompt_id"] = expect_prompt_id
        if expect_regex is not None:
            body["expect_regex"] = expect_regex
        return await self._request(
            "POST",
            f"{self._hp(worker_id, hijack_id)}/send",
            json=body,
        )

    async def step(
        self,
        worker_id: str,
        hijack_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Single-step a hijacked worker loop."""
        return await self._request(
            "POST",
            f"{self._hp(worker_id, hijack_id)}/step",
        )

    async def release(
        self,
        worker_id: str,
        hijack_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Release hijack session and resume worker automation."""
        return await self._request(
            "POST",
            f"{self._hp(worker_id, hijack_id)}/release",
        )

    async def snapshot(
        self,
        worker_id: str,
        hijack_id: str,
        *,
        wait_ms: int = 1500,
    ) -> tuple[bool, dict[str, Any]]:
        """Read terminal snapshot from an active hijack session."""
        return await self._request(
            "GET",
            f"{self._hp(worker_id, hijack_id)}/snapshot",
            params={"wait_ms": wait_ms},
        )

    async def events(
        self,
        worker_id: str,
        hijack_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> tuple[bool, dict[str, Any]]:
        """Read events from an active hijack session."""
        return await self._request(
            "GET",
            f"{self._hp(worker_id, hijack_id)}/events",
            params={"after_seq": after_seq, "limit": limit},
        )

    # -- worker control -------------------------------------------------------

    async def set_input_mode(
        self,
        worker_id: str,
        mode: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Set input mode (``"hijack"`` or ``"open"``)."""
        return await self._request(
            "POST",
            f"{self._wp(worker_id)}/input_mode",
            json={"input_mode": mode},
        )

    async def disconnect_worker(
        self,
        worker_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Disconnect the worker WebSocket."""
        return await self._request(
            "POST",
            f"{self._wp(worker_id)}/disconnect_worker",
        )

    # -- session API (/api prefix) --------------------------------------------

    async def health(self) -> tuple[bool, dict[str, Any]]:
        """Health check."""
        return await self._request("GET", "/api/health")

    async def list_sessions(self) -> tuple[bool, Any]:
        """List all sessions."""
        return await self._request("GET", "/api/sessions")

    async def get_session(self, session_id: str) -> tuple[bool, dict[str, Any]]:
        """Get a single session's status."""
        return await self._request("GET", f"/api/sessions/{session_id}")

    async def session_snapshot(self, session_id: str) -> tuple[bool, Any]:
        """Get terminal snapshot for a session."""
        return await self._request("GET", f"/api/sessions/{session_id}/snapshot")

    async def session_events(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> tuple[bool, Any]:
        """Get events for a session."""
        return await self._request(
            "GET",
            f"/api/sessions/{session_id}/events",
            params={"limit": limit},
        )

    async def watch_session_events(
        self,
        session_id: str,
        *,
        event_types: str | None = None,
        pattern: str | None = None,
        timeout_ms: int = 5000,
        max_events: int = 50,
    ) -> tuple[bool, Any]:
        """Long-poll the session event stream via the watch endpoint.

        The HTTP request timeout is set to ``timeout_ms + 5 s`` to accommodate
        the server-side wait plus network overhead.
        """
        params: dict[str, Any] = {"timeout_ms": timeout_ms, "max_events": max_events}
        if event_types is not None:
            params["event_types"] = event_types
        if pattern is not None:
            params["pattern"] = pattern
        return await self._request(
            "GET",
            f"/api/sessions/{session_id}/events/watch",
            params=params,
            timeout=timeout_ms / 1000 + 5.0,
        )

    async def set_session_mode(
        self,
        session_id: str,
        mode: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Set session input mode."""
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/mode",
            json={"input_mode": mode},
        )

    async def connect_session(self, session_id: str) -> tuple[bool, dict[str, Any]]:
        """Start/connect a session."""
        return await self._request("POST", f"/api/sessions/{session_id}/connect")

    async def disconnect_session(self, session_id: str) -> tuple[bool, dict[str, Any]]:
        """Stop/disconnect a session."""
        return await self._request("POST", f"/api/sessions/{session_id}/disconnect")

    async def quick_connect(
        self,
        connector_type: str,
        *,
        display_name: str | None = None,
        **connector_config: Any,
    ) -> tuple[bool, dict[str, Any]]:
        """Create an ephemeral session via quick-connect."""
        body: dict[str, Any] = {"connector_type": connector_type}
        if display_name is not None:
            body["display_name"] = display_name
        body.update(connector_config)
        return await self._request("POST", "/api/connect", json=body)

    async def post(self, path: str, *, json: dict[str, Any] | None = None) -> tuple[bool, Any]:
        """Generic POST helper for API paths not covered by a dedicated method."""
        return await self._request("POST", path, json=json)
