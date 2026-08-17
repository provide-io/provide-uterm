#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack-lifecycle and server/worker-control MCP tool registrations.

:func:`register_hijack_tools` registers ten tools on an
:class:`~mcp.server.mcpserver.MCPServer` instance: the six hijack-lease tools (``hijack_begin``/``heartbeat``/``read``/
``send``/``step``/``release``) plus the four server/worker control tools
(``server_health``, ``session_set_mode``, ``worker_input_mode``,
``worker_disconnect``).  It is invoked by
:func:`provide.uterm.ai.server_impl.create_mcp_app`; every handler is wrapped by
the authorization chokepoint (:mod:`provide.uterm.ai.auth`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# ``Context`` is annotation-only here, but the MCP SDK resolves tool signatures
# via ``get_type_hints`` at decoration time (``from __future__ import
# annotations`` stringifies them), so it must be importable at runtime — keep
# it out of the TYPE_CHECKING block.
from mcp.server.mcpserver import Context  # noqa: TC002

from provide.uterm.ai.auth import authorized
from provide.uterm.ai.constants import MAX_KEYSTROKE_BYTES
from provide.uterm.ai.server_validators import (
    _clean_snapshot,
    _reject_bad_id,
    _reject_bad_ids,
    _reject_bad_pattern,
)
from provide.uterm.client.mcp_tools import _ok
from provide.uterm.client.sanitizer import prepare_keystrokes

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from provide.uterm.ai.auth import AuthorizationContext
    from provide.uterm.client.hijack import HijackClient


def register_hijack_tools(
    mcp: MCPServer,
    client: HijackClient,
    auth_ctx: AuthorizationContext,
) -> None:
    """Register hijack-lifecycle and server/worker control tools on *mcp*."""

    # -- Hijack lifecycle tools -----------------------------------------------

    @mcp.tool()
    @authorized("hijack_begin", auth_ctx)
    async def hijack_begin(
        worker_id: str,
        lease_s: int = 90,
        owner: str = "operator",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Acquire a lease-based hijack session for a running worker."""
        rejection = _reject_bad_id(worker_id, "worker_id")
        if rejection is not None:
            return rejection
        ok, data = await client.acquire(worker_id, owner=owner, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_heartbeat", auth_ctx)
    async def hijack_heartbeat(
        worker_id: str,
        hijack_id: str,
        lease_s: int = 90,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Extend a hijack lease."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.heartbeat(worker_id, hijack_id, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_read", auth_ctx)
    async def hijack_read(
        worker_id: str,
        hijack_id: str,
        mode: str = "snapshot",
        output: str = "text",
        wait_ms: int = 1500,
        after_seq: int = 0,
        limit: int = 200,
        tail_lines: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Read snapshot or events from an active hijack session.

        Parameters
        ----------
        mode:
            ``"snapshot"`` for current terminal state,
            ``"events"`` for event log.
        output:
            ``"text"`` — plain text, ANSI stripped (default).
            ``"rendered"`` — visual grid with layout metadata.
            ``"raw"`` — full fidelity, ANSI intact.
        wait_ms:
            Snapshot polling timeout (snapshot mode only).
        after_seq:
            Return events after this sequence number (events mode only).
        limit:
            Max events to return (events mode only).
        tail_lines:
            When set, trim the screen text to the last N lines.
            Useful for reducing context when only recent output matters.
        """
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
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
        result = _ok(ok, data)
        if ok and mode != "events" and result.get("snapshot"):
            result["snapshot"] = _clean_snapshot(result["snapshot"], output, tail_lines=tail_lines)
        return result

    @mcp.tool()
    @authorized("hijack_send", auth_ctx)
    async def hijack_send(
        worker_id: str,
        hijack_id: str,
        keys: str,
        expect_prompt_id: str | None = None,
        expect_regex: str | None = None,
        timeout_ms: int = 2000,
        poll_interval_ms: int = 120,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Send input to a hijacked worker, optionally guarded by prompt/regex."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        # Cap the attacker-supplied expect_regex length before forwarding it to
        # the server (which also compiles it). The server must additionally
        # bound matching time — see the A4 cross-lane request.
        rejection = _reject_bad_pattern(expect_regex)
        if rejection is not None:
            return rejection
        ok, data = await client.send(
            worker_id,
            hijack_id,
            keys=prepare_keystrokes(keys, max_bytes=MAX_KEYSTROKE_BYTES),
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_step", auth_ctx)
    async def hijack_step(
        worker_id: str,
        hijack_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Single-step a hijacked worker loop."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.step(worker_id, hijack_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_release", auth_ctx)
    async def hijack_release(
        worker_id: str,
        hijack_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Release hijack session and resume worker automation."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.release(worker_id, hijack_id)
        return _ok(ok, data)

    # -- Server / worker control tools ----------------------------------------

    @mcp.tool()
    @authorized("server_health", auth_ctx)
    async def server_health(ctx: Context | None = None) -> dict[str, Any]:
        """Health check the provide-uterm server."""
        ok, data = await client.health()
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_set_mode", auth_ctx)
    async def session_set_mode(
        session_id: str,
        mode: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set session input mode (hijack/open)."""
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.set_session_mode(session_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("worker_input_mode", auth_ctx)
    async def worker_input_mode(
        worker_id: str,
        mode: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set worker input mode directly (hijack/open)."""
        rejection = _reject_bad_id(worker_id, "worker_id")
        if rejection is not None:
            return rejection
        ok, data = await client.set_input_mode(worker_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("worker_disconnect", auth_ctx)
    async def worker_disconnect(worker_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Disconnect a worker WebSocket."""
        rejection = _reject_bad_id(worker_id, "worker_id")
        if rejection is not None:
            return rejection
        ok, data = await client.disconnect_worker(worker_id)
        return _ok(ok, data)
