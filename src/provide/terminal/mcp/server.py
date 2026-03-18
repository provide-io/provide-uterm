#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastMCP server exposing the full provide-terminal control plane.

Factory function ``create_mcp_app()`` returns a ready-to-run :class:`FastMCP`
instance with ~16 tools covering session management, hijack lifecycle, and
worker control.

Usage::

    from provide.terminal.mcp import create_mcp_app

    app = create_mcp_app("http://localhost:8780")
    app.run(transport="stdio")
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastmcp import FastMCP

from provide.terminal.client.hijack import HijackClient
from provide.terminal.client.mcp_tools import _ok
from provide.terminal.screen import strip_ansi

TOOL_COUNT = 16

# C-style escape sequences that LLMs commonly emit in ``keys`` strings.
_ESCAPE_MAP: dict[str, str] = {
    r"\r": "\r",
    r"\n": "\n",
    r"\t": "\t",
    r"\x1b": "\x1b",
    r"\e": "\x1b",
    r"\\": "\\",
}


def _unescape_keys(raw: str) -> str:
    """Translate common C-style escape sequences in *raw* to real characters.

    Only sequences in :data:`_ESCAPE_MAP` are processed; everything else is
    left as-is so that arbitrary user text passes through safely.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\":
            for esc, char in _ESCAPE_MAP.items():
                if raw[i:].startswith(esc):
                    out.append(char)
                    i += len(esc)
                    break
            else:
                out.append(raw[i])
                i += 1
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _clean_snapshot(
    snapshot: dict[str, Any],
    output: str,
    *,
    tail_lines: int | None = None,
) -> dict[str, Any]:
    """Process a snapshot dict according to the requested output mode.

    Parameters
    ----------
    snapshot:
        Raw snapshot dict from the server (contains ``screen``, ``cursor``,
        ``cols``, ``rows``, etc.).
    output:
        ``"text"`` — strip ANSI, return only ``screen``.
        ``"rendered"`` — keep visual grid as-is + cursor/cols/rows metadata.
        ``"raw"`` — return full snapshot unchanged.
    tail_lines:
        When set, trim the ``screen`` text to the last *N* lines.
    """
    if output == "raw":
        if tail_lines is not None and tail_lines > 0:
            screen = snapshot.get("screen", "")
            lines = screen.splitlines()
            if len(lines) > tail_lines:
                return {**snapshot, "screen": "\n".join(lines[-tail_lines:])}
        return snapshot
    screen = strip_ansi(snapshot.get("screen", ""))
    if tail_lines is not None and tail_lines > 0:
        lines = screen.splitlines()
        if len(lines) > tail_lines:
            screen = "\n".join(lines[-tail_lines:])
    if output == "text":
        return {"screen": screen}
    # rendered: visual grid intact, strip ANSI, include layout metadata
    result: dict[str, Any] = {"screen": screen}
    for key in ("cursor", "cols", "rows"):
        if key in snapshot:
            result[key] = snapshot[key]
    return result


def create_mcp_app(base_url: str, **client_kwargs: Any) -> FastMCP:
    """Create a FastMCP app with all provide-terminal tools.

    Parameters
    ----------
    base_url:
        Root URL of the provide-terminal server.
    **client_kwargs:
        Forwarded to :class:`HijackClient` (``entity_prefix``,
        ``headers``, ``timeout``, ``transport``).
    """
    client = HijackClient(base_url, **client_kwargs)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
        yield
        await client.__aexit__(None, None, None)

    mcp = FastMCP("uterm", lifespan=_lifespan)

    # -- Hijack lifecycle tools -----------------------------------------------

    @mcp.tool()
    async def hijack_begin(
        worker_id: str,
        lease_s: int = 90,
        owner: str = "operator",
    ) -> dict[str, Any]:
        """Acquire a lease-based hijack session for a running worker."""
        ok, data = await client.acquire(worker_id, owner=owner, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    async def hijack_heartbeat(
        worker_id: str,
        hijack_id: str,
        lease_s: int = 90,
    ) -> dict[str, Any]:
        """Extend a hijack lease."""
        ok, data = await client.heartbeat(worker_id, hijack_id, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    async def hijack_read(
        worker_id: str,
        hijack_id: str,
        mode: str = "snapshot",
        output: str = "text",
        wait_ms: int = 1500,
        after_seq: int = 0,
        limit: int = 200,
        tail_lines: int | None = None,
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
        ok, data = await client.send(
            worker_id,
            hijack_id,
            keys=_unescape_keys(keys),
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )
        return _ok(ok, data)

    @mcp.tool()
    async def hijack_step(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Single-step a hijacked worker loop."""
        ok, data = await client.step(worker_id, hijack_id)
        return _ok(ok, data)

    @mcp.tool()
    async def hijack_release(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Release hijack session and resume worker automation."""
        ok, data = await client.release(worker_id, hijack_id)
        return _ok(ok, data)

    # -- Session management tools ---------------------------------------------

    @mcp.tool()
    async def session_list() -> dict[str, Any]:
        """List all sessions with status."""
        ok, data = await client.list_sessions()
        return _ok(ok, data)

    @mcp.tool()
    async def session_status(session_id: str) -> dict[str, Any]:
        """Get a single session's details."""
        ok, data = await client.get_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    async def session_read(
        session_id: str,
        output: str = "text",
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        """Get terminal snapshot for a session.

        Parameters
        ----------
        output:
            ``"text"`` — plain text, ANSI stripped (default).
            ``"rendered"`` — visual grid with layout metadata.
            ``"raw"`` — full fidelity, ANSI intact.
        tail_lines:
            When set, trim the screen text to the last N lines.
        """
        ok, data = await client.session_snapshot(session_id)
        result = _ok(ok, data)
        if ok and result.get("snapshot"):
            result["snapshot"] = _clean_snapshot(result["snapshot"], output, tail_lines=tail_lines)
        return result

    @mcp.tool()
    async def session_connect(session_id: str) -> dict[str, Any]:
        """Start/connect a session."""
        ok, data = await client.connect_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    async def session_disconnect(session_id: str) -> dict[str, Any]:
        """Stop/disconnect a session."""
        ok, data = await client.disconnect_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    async def session_create(
        connector_type: str,
        display_name: str | None = None,
        host: str | None = None,
        port: int | None = None,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        input_mode: str | None = None,
    ) -> dict[str, Any]:
        """Create an ephemeral session via quick-connect."""
        kwargs: dict[str, Any] = {}
        if display_name is not None:
            kwargs["display_name"] = display_name
        if host is not None:
            kwargs["host"] = host
        if port is not None:
            kwargs["port"] = port
        if url is not None:
            kwargs["url"] = url
        if username is not None:
            kwargs["username"] = username
        if password is not None:
            kwargs["password"] = password
        if input_mode is not None:
            kwargs["input_mode"] = input_mode
        ok, data = await client.quick_connect(connector_type, **kwargs)
        return _ok(ok, data)

    # -- Server / worker control tools ----------------------------------------

    @mcp.tool()
    async def server_health() -> dict[str, Any]:
        """Health check the provide-terminal server."""
        ok, data = await client.health()
        return _ok(ok, data)

    @mcp.tool()
    async def session_set_mode(
        session_id: str,
        mode: str,
    ) -> dict[str, Any]:
        """Set session input mode (hijack/open)."""
        ok, data = await client.set_session_mode(session_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    async def worker_input_mode(
        worker_id: str,
        mode: str,
    ) -> dict[str, Any]:
        """Set worker input mode directly (hijack/open)."""
        ok, data = await client.set_input_mode(worker_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    async def worker_disconnect(worker_id: str) -> dict[str, Any]:
        """Disconnect a worker WebSocket."""
        ok, data = await client.disconnect_worker(worker_id)
        return _ok(ok, data)

    return mcp
