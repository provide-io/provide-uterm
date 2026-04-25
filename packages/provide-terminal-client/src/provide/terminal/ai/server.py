#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastMCP server exposing the full provide-terminal control plane.

Factory function ``create_mcp_app()`` returns a ready-to-run :class:`FastMCP`
instance with 21 tools covering session management, hijack lifecycle, and
worker control.

Usage::

    from provide.terminal.ai import create_mcp_app

    app = create_mcp_app("http://localhost:8780")
    app.run(transport="stdio")
"""

from __future__ import annotations

import codecs
import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastmcp import FastMCP
from provide.terminal.screen import strip_ansi

from provide.terminal.client.hijack import HijackClient
from provide.terminal.client.mcp_tools import _ok

TOOL_COUNT = 21


def _unescape_keys(raw: str) -> str:
    """Translate C-style and Unicode escape sequences in *raw* to real characters."""
    try:
        # encode to bytes then decode with unicode_escape to handle \n, \r, \xNN, \uNNNN, etc.
        return codecs.decode(raw.encode("utf-8"), "unicode_escape")
    except Exception:
        # Fallback to no-op if decoding fails (e.g. malformed escape at end of string)
        return raw


def _trim_tail(screen: str, tail_lines: int | None) -> str:
    """Trim *screen* to the last *tail_lines* lines (no-op when tail_lines is unset)."""
    if tail_lines is not None and tail_lines > 0:
        lines = screen.splitlines()
        if len(lines) > tail_lines:
            return "\n".join(lines[-tail_lines:])
    return screen


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
    screen = _trim_tail(strip_ansi(snapshot.get("screen", "")), tail_lines)
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

    # -- Real-time event subscription -----------------------------------------

    @mcp.tool()
    async def session_watch(
        session_id: str,
        event_types: str | None = None,
        pattern: str | None = None,
        timeout_s: float = 10.0,
        max_events: int = 50,
    ) -> dict[str, Any]:
        """Watch a session for events in real time.

        Subscribes to the session event stream and returns events as they arrive.
        When the server's EventBus is not configured, returns recent events from
        the ring buffer instead (graceful fallback).

        Parameters
        ----------
        event_types:
            Comma-separated list of event types to filter on
            (e.g. ``"snapshot,input_send"``).  Omit to receive all types.
        pattern:
            Regex applied to ``snapshot`` event ``data.screen`` text.
            Only matching snapshots are returned.
        timeout_s:
            How long to wait for events before returning (clamped to 30 s).
        max_events:
            Maximum events to collect before returning early.
        """
        ok, data = await client.watch_session_events(
            session_id,
            event_types=event_types,
            pattern=pattern,
            timeout_ms=int(min(max(timeout_s, 0.1), 30) * 1000),
            max_events=max_events,
        )
        return _ok(ok, data)

    @mcp.tool()
    async def session_subscribe(
        session_id: str,
        event_types: str | None = None,
        pattern: str | None = None,
        duration_s: float = 30.0,
        max_events: int = 200,
    ) -> dict[str, Any]:
        """Long-running session subscription for agent loops.

        Unlike ``session_watch`` (≤ 30 s, ≤ 50 events), this tool is designed
        for AI agents that need to monitor a session for an extended period —
        for example, waiting for a shell prompt regex to appear before sending
        the next command.

        Returns when *max_events* events have been collected, the *pattern*
        fires at least once, or *duration_s* elapses — whichever comes first.

        Parameters
        ----------
        event_types:
            Comma-separated list of event types to filter on
            (e.g. ``"snapshot"``).  Omit to receive all types.
        pattern:
            Regex applied to ``snapshot`` event ``data.screen`` text.
            Only matching snapshots are returned.  When this fires,
            ``matched_pattern`` will be ``True`` in the response.
        duration_s:
            How long to subscribe before returning (clamped to 1-120 s).
        max_events:
            Maximum events to collect before returning early (clamped to
            1-500).
        """
        clamped_duration_s = min(max(duration_s, 1.0), 120.0)
        clamped_max_events = min(max(max_events, 1), 500)
        ok, data = await client.watch_session_events(
            session_id,
            event_types=event_types,
            pattern=pattern,
            timeout_ms=int(clamped_duration_s * 1000),
            max_events=clamped_max_events,
        )
        # Enrich with matched_pattern so callers know whether the pattern fired.
        matched = bool(pattern and ok and data.get("events"))
        result = _ok(ok, data)
        result["matched_pattern"] = matched
        return result

    # -- Fan-out tools --------------------------------------------------------

    @mcp.tool()
    async def fanout_group_create(
        session_ids: list[str],
        name: str = "fleet",
        mode: str = "parallel",
    ) -> dict[str, Any]:
        """Create a fan-out group to broadcast input to multiple sessions simultaneously."""
        ok, data = await client.post(
            "/api/fanout/groups",
            json={"name": name, "worker_ids": session_ids, "mode": mode},
        )
        return _ok(ok, data)

    @mcp.tool()
    async def fanout_send(
        group_id: str,
        data: str,
        quiesce_ms: int = 500,
        max_response_ms: int = 10000,
    ) -> dict[str, Any]:
        """Broadcast input to all sessions in a fan-out group and return per-session results with divergence detection."""
        ok, result = await client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={"data": data, "quiesce_ms": quiesce_ms, "max_response_ms": max_response_ms},
        )
        return _ok(ok, result)

    # -- Session annotation tool ----------------------------------------------

    @mcp.tool()
    async def session_annotate(
        session_id: str,
        label: str,
        description: str = "",
        severity: str = "info",
    ) -> dict[str, Any]:
        """Add an annotation to a session's recording timeline. Use this to mark important moments."""
        ok, data = await client.post(
            f"/api/sessions/{session_id}/annotate",
            json={"label": label, "description": description, "severity": severity},
        )
        return _ok(ok, data)

    return mcp
