#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic MCP tool functions for the hijack lifecycle.

These are plain async functions — not tied to any MCP framework.
Consumers register them into their own MCP server/registry::

    from provide.uterm.client.mcp_tools import hijack_tools
    for tool_fn in hijack_tools(base_url):
        registry.tool()(tool_fn)
"""

from __future__ import annotations

from typing import Any

from provide.uterm.client.hijack import HijackClient


def _ok(ok: bool, data: Any) -> dict[str, Any]:
    """Normalise ``(bool, dict)`` into a single MCP-friendly dict."""
    if isinstance(data, dict):
        return {"success": ok, **data}
    return {"success": ok, "data": data}


def hijack_tools(base_url: str, **client_kwargs: Any) -> list[Any]:
    """Return MCP tool functions pre-bound to a :class:`HijackClient`.

    Each function has docstrings, type hints, and parameter defaults
    suitable for direct registration as an MCP tool.

    Parameters
    ----------
    base_url:
        Root URL of the provide-uterm server.
    **client_kwargs:
        Forwarded to :class:`HijackClient` (e.g. ``entity_prefix``,
        ``headers``, ``timeout``).
    """
    client = HijackClient(base_url, **client_kwargs)

    async def hijack_begin(
        worker_id: str,
        lease_s: int = 90,
        owner: str = "operator",
    ) -> dict[str, Any]:
        """Acquire a lease-based hijack session for a running worker."""
        ok, data = await client.acquire(worker_id, owner=owner, lease_s=lease_s)
        return _ok(ok, data)

    async def hijack_heartbeat(
        worker_id: str,
        hijack_id: str,
        lease_s: int = 90,
    ) -> dict[str, Any]:
        """Extend a hijack lease."""
        ok, data = await client.heartbeat(worker_id, hijack_id, lease_s=lease_s)
        return _ok(ok, data)

    async def hijack_read(
        worker_id: str,
        hijack_id: str,
        mode: str = "snapshot",
        wait_ms: int = 1500,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read snapshot or events from an active hijack session.

        Parameters
        ----------
        mode:
            ``"snapshot"`` for current terminal state,
            ``"events"`` for event log.
        wait_ms:
            Snapshot polling timeout (snapshot mode only).
        after_seq:
            Return events after this sequence number (events mode only).
        limit:
            Max events to return (events mode only).
        """
        if mode == "events":
            ok, data = await client.events(
                worker_id,
                hijack_id,
                after_seq=after_seq,
                limit=limit,
            )
        else:
            ok, data = await client.snapshot(
                worker_id,
                hijack_id,
                wait_ms=wait_ms,
            )
        return _ok(ok, data)

    async def hijack_send(
        worker_id: str,
        hijack_id: str,
        keys: str,
        expect_prompt_id: str | None = None,
        expect_regex: str | None = None,
        timeout_ms: int = 2000,
        poll_interval_ms: int = 120,
    ) -> dict[str, Any]:
        """Send input to a hijacked worker, optionally guarded by prompt/regex."""
        from provide.uterm.client.sanitizer import prepare_keystrokes

        safe_keys = prepare_keystrokes(keys)
        ok, data = await client.send(
            worker_id,
            hijack_id,
            keys=safe_keys,
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )
        return _ok(ok, data)

    async def hijack_step(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Single-step a hijacked worker loop."""
        ok, data = await client.step(worker_id, hijack_id)
        return _ok(ok, data)

    async def hijack_release(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Release hijack session and resume worker automation."""
        ok, data = await client.release(worker_id, hijack_id)
        return _ok(ok, data)

    return [hijack_begin, hijack_heartbeat, hijack_read, hijack_send, hijack_step, hijack_release]
