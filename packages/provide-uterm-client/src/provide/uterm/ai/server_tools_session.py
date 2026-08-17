#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Session-management, event-subscription, fan-out, and annotation MCP tools.

:func:`register_session_tools` registers eleven tools on an
:class:`~mcp.server.mcpserver.MCPServer` instance: session management (``session_list``/``status``/``read``/``connect``/
``disconnect``/``create``), real-time event subscription
(``session_watch``/``session_subscribe``), fan-out (``fanout_group_create``/
``fanout_send``), and ``session_annotate``.  It is invoked by
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
from provide.uterm.ai.server_validators import (
    _clean_snapshot,
    _compiled_pattern_or_rejection,
    _reject_bad_id,
    _reject_bad_pattern,
    _validate_session_create_config,
)
from provide.uterm.client.mcp_tools import _ok

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from provide.uterm.ai.auth import AuthorizationContext
    from provide.uterm.client.hijack import HijackClient


def register_session_tools(
    mcp: MCPServer,
    client: HijackClient,
    auth_ctx: AuthorizationContext,
) -> None:
    """Register session, watch/subscribe, fan-out, and annotation tools on *mcp*."""

    # -- Session management tools ---------------------------------------------

    @mcp.tool()
    @authorized("session_list", auth_ctx)
    async def session_list(ctx: Context | None = None) -> dict[str, Any]:
        """List all sessions with status."""
        ok, data = await client.list_sessions()
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_status", auth_ctx)
    async def session_status(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Get a single session's details."""
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.get_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_read", auth_ctx)
    async def session_read(
        session_id: str,
        output: str = "text",
        tail_lines: int | None = None,
        ctx: Context | None = None,
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
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.session_snapshot(session_id)
        result = _ok(ok, data)
        if ok and result.get("snapshot"):
            result["snapshot"] = _clean_snapshot(result["snapshot"], output, tail_lines=tail_lines)
        return result

    @mcp.tool()
    @authorized("session_connect", auth_ctx)
    async def session_connect(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Start/connect a session."""
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.connect_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_disconnect", auth_ctx)
    async def session_disconnect(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Stop/disconnect a session."""
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.disconnect_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_create", auth_ctx)
    async def session_create(
        connector_type: str,
        display_name: str | None = None,
        host: str | None = None,
        port: int | None = None,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        input_mode: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create an ephemeral session via quick-connect."""
        # Vet the connector config before any RPC.  ``session_create`` is
        # the broadest-blast-radius tool — it can spawn arbitrary connectors
        # — so we enforce an allowlist + field validation here.
        rejection = _validate_session_create_config(
            connector_type=connector_type,
            url=url,
            port=port,
            host=host,
        )
        if rejection is not None:
            return rejection

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

    # -- Real-time event subscription -----------------------------------------

    @mcp.tool()
    @authorized("session_watch", auth_ctx)
    async def session_watch(
        session_id: str,
        event_types: str | None = None,
        pattern: str | None = None,
        timeout_s: float = 10.0,
        max_events: int = 50,
        ctx: Context | None = None,
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
            Maximum events to collect before returning early (clamped to
            1-50).
        """
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        rejection = _reject_bad_pattern(pattern)
        if rejection is not None:
            return rejection
        # Clamp max_events symmetrically with session_subscribe so an LLM
        # cannot ask the server to collect an unbounded number of events.
        # Watch is the short-lived tool, so its ceiling matches the default.
        clamped_max_events = min(max(max_events, 1), 50)
        ok, data = await client.watch_session_events(
            session_id,
            event_types=event_types,
            pattern=pattern,
            timeout_ms=int(min(max(timeout_s, 0.1), 30) * 1000),
            max_events=clamped_max_events,
        )
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_subscribe", auth_ctx)
    async def session_subscribe(
        session_id: str,
        event_types: str | None = None,
        pattern: str | None = None,
        duration_s: float = 30.0,
        max_events: int = 200,
        ctx: Context | None = None,
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
        # Bound the attacker-supplied pattern up front (length cap → ReDoS
        # mitigation) before any compile/match work happens.
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        compiled_pattern, rejection = _compiled_pattern_or_rejection(pattern)
        if rejection is not None:
            return rejection
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
        # Re-check each event's screen text against the regex compiled above
        # (reused, not recompiled) rather than trusting that "events arrived"
        # implies "pattern matched" — the registry fallback path (no EventBus)
        # does not pre-filter events.
        matched = False
        if compiled_pattern is not None and ok:
            for event in data.get("events", []):
                if not isinstance(event, dict):
                    continue
                payload = event.get("data") or {}
                screen = payload.get("screen", "") if isinstance(payload, dict) else ""
                if not isinstance(screen, str):
                    screen = str(screen)
                if compiled_pattern.search(screen):
                    matched = True
                    break
        result = _ok(ok, data)
        result["matched_pattern"] = matched
        return result

    # -- Fan-out tools --------------------------------------------------------

    @mcp.tool()
    @authorized("fanout_group_create", auth_ctx)
    async def fanout_group_create(
        session_ids: list[str],
        name: str = "fleet",
        mode: str = "parallel",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a fan-out group to broadcast input to multiple sessions simultaneously."""
        ok, data = await client.post(
            "/api/fanout/groups",
            json={"name": name, "worker_ids": session_ids, "mode": mode},
        )
        return _ok(ok, data)

    @mcp.tool()
    @authorized("fanout_send", auth_ctx)
    async def fanout_send(
        group_id: str,
        data: str,
        quiesce_ms: int = 500,
        max_response_ms: int = 10000,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Broadcast input to all sessions in a fan-out group and return per-session results with divergence detection."""
        rejection = _reject_bad_id(group_id, "group_id")
        if rejection is not None:
            return rejection
        ok, result = await client.post(
            f"/api/fanout/groups/{group_id}/send",
            json={"data": data, "quiesce_ms": quiesce_ms, "max_response_ms": max_response_ms},
        )
        return _ok(ok, result)

    # -- Session annotation tool ----------------------------------------------

    @mcp.tool()
    @authorized("session_annotate", auth_ctx)
    async def session_annotate(
        session_id: str,
        label: str,
        description: str = "",
        severity: str = "info",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add an annotation to a session's recording timeline. Use this to mark important moments."""
        rejection = _reject_bad_id(session_id, "session_id")
        if rejection is not None:
            return rejection
        ok, data = await client.post(
            f"/api/sessions/{session_id}/annotate",
            json={"label": label, "description": description, "severity": severity},
        )
        return _ok(ok, data)
