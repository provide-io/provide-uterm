#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""GUI control MCP tool registrations.

:func:`register_gui_tools` registers five tools on a :class:`FastMCP`
instance for GUI interactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import Context  # noqa: TC002

from provide.uterm.ai.auth import authorized
from provide.uterm.ai.server_validators import _reject_bad_id, _reject_bad_ids
from provide.uterm.client.mcp_tools import _ok

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from provide.uterm.ai.auth import AuthorizationContext
    from provide.uterm.client.hijack import HijackClient


def register_gui_tools(
    mcp: FastMCP,
    client: HijackClient,
    auth_ctx: AuthorizationContext,
) -> None:
    """Register GUI interaction tools on *mcp*."""

    @mcp.tool()
    @authorized("gui_hijack_begin", auth_ctx)
    async def gui_hijack_begin(
        worker_id: str,
        lease_s: int = 90,
        owner: str = "operator",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Acquire a lease-based hijack session for GUI interactions."""
        rejection = _reject_bad_id(worker_id, "worker_id")
        if rejection is not None:
            return rejection
        ok, data = await client.acquire(worker_id, owner=owner, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_hijack_release", auth_ctx)
    async def gui_hijack_release(
        worker_id: str,
        hijack_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Release GUI hijack session."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.release(worker_id, hijack_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_screenshot", auth_ctx)
    async def gui_screenshot(
        worker_id: str,
        hijack_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Take a screenshot of the GUI."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.gui_screenshot(worker_id, hijack_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_click", auth_ctx)
    async def gui_click(
        worker_id: str,
        hijack_id: str,
        x: int,
        y: int,
        button: str = "left",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Click on the GUI."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.gui_click(worker_id, hijack_id, x=x, y=y, button=button)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_type", auth_ctx)
    async def gui_type(
        worker_id: str,
        hijack_id: str,
        text: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Type text on the GUI."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.gui_type(worker_id, hijack_id, text=text)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_key", auth_ctx)
    async def gui_key(
        worker_id: str,
        hijack_id: str,
        key_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Send GUI key event."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.gui_key(worker_id, hijack_id, key_name=key_name)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("gui_drag", auth_ctx)
    async def gui_drag(
        worker_id: str,
        hijack_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Send GUI drag event."""
        rejection = _reject_bad_ids((worker_id, "worker_id"), (hijack_id, "hijack_id"))
        if rejection is not None:
            return rejection
        ok, data = await client.gui_drag(
            worker_id, hijack_id, start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y
        )
        return _ok(ok, data)
