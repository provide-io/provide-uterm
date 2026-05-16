#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastMCP server exposing the full provide-uterm control plane.

Factory function ``create_mcp_app()`` returns a ready-to-run :class:`FastMCP`
instance with 21 tools covering session management, hijack lifecycle, and
worker control.

Every tool handler is wrapped by the authorization chokepoint
(:mod:`provide.uterm.ai.auth`); roles are declared once in
:mod:`provide.uterm.ai.policy` and an unguarded tool will be refused by
the dispatcher rather than silently exposed.

Usage::

    from provide.uterm.ai import create_mcp_app

    app = create_mcp_app("http://localhost:8780")
    app.run(transport="stdio")
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastmcp import FastMCP
from provide.uterm.screen import strip_ansi

from provide.uterm.ai.auth import (
    AuthorizationContext,
    McpPrincipal,
    authorized,
    principal_from_headers,
)
from provide.uterm.ai.policy import is_allowed_connector
from provide.uterm.client.hijack import HijackClient
from provide.uterm.client.mcp_tools import _ok

TOOL_COUNT = 21


_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "e": "\x1b",
    "0": "\x00",
    "\\": "\\",
    "'": "'",
    '"': '"',
}

_ESCAPE_PATTERN = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))",
    re.DOTALL,
)


def _unescape_keys(raw: str) -> str:
    """Translate terminal-relevant escape sequences in *raw* to real characters.

    Recognises ``\\n``, ``\\r``, ``\\t``, ``\\e``, ``\\0``, ``\\\\``, ``\\'``,
    ``\\"``, ``\\xNN`` and ``\\uNNNN``. Unknown single-letter escapes such as
    ``\\a``, ``\\b``, ``\\c``, ``\\q`` are left untouched (passed through as
    the original two-character backslash sequence) so that callers may safely
    embed literal text without surprise translation.
    """

    def _replace(match: re.Match[str]) -> str:
        hex2, hex4, ch = match.groups()
        if hex2 is not None:
            return chr(int(hex2, 16))
        if hex4 is not None:
            return chr(int(hex4, 16))
        if ch in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[ch]
        # Unknown escape — preserve the original sequence verbatim.
        return match.group(0)

    return _ESCAPE_PATTERN.sub(_replace, raw)


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


def _validate_session_create_config(
    *,
    connector_type: str,
    url: str | None,
    port: int | None,
) -> dict[str, Any] | None:
    """Vet a ``session_create`` request against the connector allowlist.

    Returns ``None`` when the config is acceptable, or a structured error
    dict (matching the rest of the MCP tool surface) when the request must
    be refused.  Validation rules:

    * ``connector_type`` must be on
      :data:`~provide.uterm.ai.policy.ALLOWED_CONNECTOR_TYPES`.
    * When supplied, ``port`` must be a TCP port in the legal range
      (1..65535).
    * When supplied, ``url`` must use a vetted scheme; arbitrary
      ``file://`` / ``javascript:`` / etc. are rejected so an MCP client
      cannot ask the worker to open a malicious resource.
    """
    if not is_allowed_connector(connector_type):
        return {
            "success": False,
            "error": "invalid_connector_type",
            "connector_type": connector_type,
        }
    if port is not None and not (1 <= port <= 65535):
        return {
            "success": False,
            "error": "invalid_port",
            "port": port,
        }
    if url is not None:
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme not in {"ws", "wss", "http", "https", "telnet", "ssh"}:
            return {
                "success": False,
                "error": "invalid_url_scheme",
                "scheme": scheme or "<missing>",
            }
    return None


def create_mcp_app(
    base_url: str,
    *,
    default_principal: McpPrincipal | None = None,
    **client_kwargs: Any,
) -> FastMCP:
    """Create a FastMCP app with all provide-uterm tools.

    Parameters
    ----------
    base_url:
        Root URL of the provide-uterm server.
    default_principal:
        Principal applied when no per-request authentication is available.
        When ``None``, the principal is inferred from the ``X-Uterm-Principal``
        / ``X-Uterm-Role`` headers in ``client_kwargs["headers"]`` (so legacy
        callers that supplied auth headers continue to work), falling back to
        an admin principal for stdio/local development.
    **client_kwargs:
        Forwarded to :class:`HijackClient` (``entity_prefix``,
        ``headers``, ``timeout``, ``transport``).
    """
    client = HijackClient(base_url, **client_kwargs)

    if default_principal is None:
        default_principal = principal_from_headers(client_kwargs.get("headers")) or McpPrincipal(
            subject_id="local",
            roles=frozenset({"admin"}),
        )
    auth_ctx = AuthorizationContext(default_principal=default_principal)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
        yield
        await client.__aexit__(None, None, None)

    mcp = FastMCP("uterm", lifespan=_lifespan)

    # -- Hijack lifecycle tools -----------------------------------------------

    @mcp.tool()
    @authorized("hijack_begin", auth_ctx)
    async def hijack_begin(
        worker_id: str,
        lease_s: int = 90,
        owner: str = "operator",
    ) -> dict[str, Any]:
        """Acquire a lease-based hijack session for a running worker."""
        ok, data = await client.acquire(worker_id, owner=owner, lease_s=lease_s)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_heartbeat", auth_ctx)
    async def hijack_heartbeat(
        worker_id: str,
        hijack_id: str,
        lease_s: int = 90,
    ) -> dict[str, Any]:
        """Extend a hijack lease."""
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
    @authorized("hijack_send", auth_ctx)
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
    @authorized("hijack_step", auth_ctx)
    async def hijack_step(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Single-step a hijacked worker loop."""
        ok, data = await client.step(worker_id, hijack_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("hijack_release", auth_ctx)
    async def hijack_release(
        worker_id: str,
        hijack_id: str,
    ) -> dict[str, Any]:
        """Release hijack session and resume worker automation."""
        ok, data = await client.release(worker_id, hijack_id)
        return _ok(ok, data)

    # -- Session management tools ---------------------------------------------

    @mcp.tool()
    @authorized("session_list", auth_ctx)
    async def session_list() -> dict[str, Any]:
        """List all sessions with status."""
        ok, data = await client.list_sessions()
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_status", auth_ctx)
    async def session_status(session_id: str) -> dict[str, Any]:
        """Get a single session's details."""
        ok, data = await client.get_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_read", auth_ctx)
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
    @authorized("session_connect", auth_ctx)
    async def session_connect(session_id: str) -> dict[str, Any]:
        """Start/connect a session."""
        ok, data = await client.connect_session(session_id)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_disconnect", auth_ctx)
    async def session_disconnect(session_id: str) -> dict[str, Any]:
        """Stop/disconnect a session."""
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
    ) -> dict[str, Any]:
        """Create an ephemeral session via quick-connect."""
        # Vet the connector config before any RPC.  ``session_create`` is
        # the broadest-blast-radius tool — it can spawn arbitrary connectors
        # — so we enforce an allowlist + field validation here.
        rejection = _validate_session_create_config(
            connector_type=connector_type,
            url=url,
            port=port,
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

    # -- Server / worker control tools ----------------------------------------

    @mcp.tool()
    @authorized("server_health", auth_ctx)
    async def server_health() -> dict[str, Any]:
        """Health check the provide-uterm server."""
        ok, data = await client.health()
        return _ok(ok, data)

    @mcp.tool()
    @authorized("session_set_mode", auth_ctx)
    async def session_set_mode(
        session_id: str,
        mode: str,
    ) -> dict[str, Any]:
        """Set session input mode (hijack/open)."""
        ok, data = await client.set_session_mode(session_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("worker_input_mode", auth_ctx)
    async def worker_input_mode(
        worker_id: str,
        mode: str,
    ) -> dict[str, Any]:
        """Set worker input mode directly (hijack/open)."""
        ok, data = await client.set_input_mode(worker_id, mode)
        return _ok(ok, data)

    @mcp.tool()
    @authorized("worker_disconnect", auth_ctx)
    async def worker_disconnect(worker_id: str) -> dict[str, Any]:
        """Disconnect a worker WebSocket."""
        ok, data = await client.disconnect_worker(worker_id)
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
    @authorized("session_subscribe", auth_ctx)
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
    @authorized("fanout_group_create", auth_ctx)
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
    @authorized("fanout_send", auth_ctx)
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
    @authorized("session_annotate", auth_ctx)
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
