#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
Server-side PAM event integration.

Wires PamNotifyListener (provide-uterm-platform) into the session registry so that
sshd logins tracked by pam_uterm.so automatically become provide-uterm sessions.

Two modes, configured via ``ServerConfig.pam.mode``:

  notify (default)
    pam_uterm.so sends a JSON notification.  The server receives it, logs it,
    and — when ``pam.auto_session`` is true — auto-creates a *new* shell as
    the authenticated user (a parallel companion session, not the SSH session
    itself).

  capture
    pam_uterm.so sends the notification AND injects ``LD_PRELOAD=libuterm_capture.so``
    + ``UTERM_CAPTURE_SOCKET=/run/uterm-cap-{pid}.sock`` into the login
    environment.  The server pre-allocates a CaptureSocket at that path so the
    shell's I/O flows directly into a read-only session in the registry — this
    IS the live SSH session, observable via the provide-uterm UI.

The integration starts at server startup and runs until the server shuts down.
It is fully opt-in: nothing happens unless ``config.pam.notify_socket`` is set.

Session ids
  A PAM event names its login by the pid of the process that ran PAM, and pids
  are reused. An id built from the pid alone was handed, with everything
  recorded under it, to whichever later login drew the same pid, so every id
  that carries a pid also carries that process's start time (``/proc/<pid>/stat``
  field 22, clock ticks since boot): a pid is reused, a pid together with when
  it started is not. ``pam-<user>-capture-<pid>-<start>`` for a capture
  session, ``pam-<user>-tty-<pid>-<start>`` for a notify session with no tty.
  Where the start time cannot be read, a random ``r<hex>`` takes its place.
  The id is computed once, at open, and remembered for the close: by the time a
  close is handled the process may already be gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.pty.pam_listener import PamEvent
    from provide.uterm.server.models import PamConfig, ServerConfig
    from provide.uterm.server.registry import SessionRegistry

logger = logging.getLogger(__name__)

_TTY_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


async def _forward_to_relay(event_json: dict[str, object], relay_url: str, relay_token: str) -> None:
    """POST PAM event to relay service /api/pam-events. Best-effort — never raises."""
    url = relay_url.rstrip("/") + "/api/pam-events"
    try:
        from provide.uterm.server import _http
        from provide.uterm.server.egress import assert_webhook_target_allowed

        # L11: guard the outbound POST against SSRF/exfiltration before sending —
        # a relay_url pointing at a cloud-metadata IP (or a rebound internal host)
        # would otherwise leak PAM event data + the relay bearer token. On a
        # blocked target this raises EgressBlockedError, which the except below
        # already swallows (log + skip the POST), so the PAM loop never crashes.
        await assert_webhook_target_allowed(relay_url)

        async with _http.async_client(timeout=5.0) as client:
            await client.post(
                url,
                json=event_json,
                headers={"Authorization": f"Bearer {relay_token}"},
            )
    except Exception as exc:
        logger.warning("pam_relay_forward_failed url=%s error=%s", url, exc)


async def _create_relay_tunnel(
    relay_url: str, relay_token: str, session_id: str, display_name: str
) -> tuple[str, str] | None:
    """POST /api/tunnels → (worker_token, ws_endpoint). Returns None on failure."""
    url = relay_url.rstrip("/") + "/api/tunnels"
    try:
        from provide.uterm.server import _http
        from provide.uterm.server.egress import assert_webhook_target_allowed

        # L11: same egress guard as _forward_to_relay — refuse to POST tunnel
        # provisioning (which carries the relay bearer token) to a metadata IP or
        # rebound internal host. EgressBlockedError lands in the except below,
        # which logs and returns None — the existing failure path for this call.
        await assert_webhook_target_allowed(relay_url)

        async with _http.async_client(timeout=5.0) as client:
            resp = await client.post(
                url,
                json={
                    "session_id": session_id,
                    "display_name": display_name,
                    "tunnel_type": "terminal",
                },
                headers={"Authorization": f"Bearer {relay_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["worker_token"]), str(data["ws_endpoint"])
    except Exception as exc:
        logger.warning("create_relay_tunnel_failed url=%s error=%s", url, exc)
        return None


def _tty_slug(tty: str) -> str:
    """'/dev/pts/3' → 'pts-3'."""
    basename = tty.split("/")[-1] if "/" in tty else tty
    return _TTY_SLUG_RE.sub("-", basename).strip("-") or "tty"


def _process_start_ticks(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot, or None if /proc will not say.

    Field 22 of ``/proc/<pid>/stat``. The command name before it is in
    parentheses and may itself contain spaces and parentheses; every field after
    the LAST ")" is fixed, which puts the start time at offset 19 there.
    """
    if pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = text.rpartition(")")[2].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _instance_tag(pid: int) -> str:
    """What tells this login's process apart from every other that had its pid."""
    ticks = _process_start_ticks(pid)
    return str(ticks) if ticks is not None else f"r{secrets.token_hex(4)}"


def _session_id(ev: PamEvent) -> str:
    """A new session id for the login a PAM event names (see the module docstring).

    Every id carrying a pid also carries the instance tag, so a later login that
    draws the same pid gets a different id. A tty-named notify id is unchanged:
    it is the shape the relay's ``/api/pam-events`` derives for the same login.
    Computed once per login, at open (see :func:`_on_open`).
    """
    if ev.mode == "capture" or ev.capture_socket is not None:
        return f"pam-{ev.username}-capture-{ev.pid}-{_instance_tag(ev.pid)}"
    slug = _tty_slug(ev.tty)
    if not ev.tty:
        return f"pam-{ev.username}-{slug}-{ev.pid}-{_instance_tag(ev.pid)}"
    return f"pam-{ev.username}-{slug}"


def _login_key(ev: PamEvent) -> tuple[str, int]:
    """What an open and its close have in common: the user and the PAM process."""
    return (ev.username, ev.pid)


async def run_pam_integration(config: ServerConfig, registry: SessionRegistry) -> None:
    """
    Long-running coroutine: start PamNotifyListener and dispatch events.

    Wrap in ``asyncio.create_task()``.  Cancelled cleanly on server shutdown.
    """
    pam_cfg = config.pam
    if not pam_cfg.notify_socket:
        return

    try:
        from provide.uterm.pty.pam_listener import PamNotifyListener
    except ImportError:
        logger.warning("pam_integration: provide-uterm-platform not installed — PAM listener disabled")
        return

    _bridges: dict[str, object] = {}
    # The id each open login was given, for its close to find (see _on_open).
    _instances: dict[tuple[str, int], str] = {}

    async def handle(event: PamEvent) -> None:
        ev = event
        logger.info(
            "pam_event event=%s username=%s tty=%s pid=%d mode=%s",
            ev.event,
            ev.username,
            ev.tty,
            ev.pid,
            ev.mode,
        )
        if ev.event == "open":
            await _on_open(ev, pam_cfg, registry, _bridges, _instances)
        elif ev.event == "close":  # pragma: no branch
            await _on_close(ev, pam_cfg, registry, _bridges, _instances)

    listener = PamNotifyListener(pam_cfg.notify_socket, require_peer_uids=pam_cfg.require_peer_uids)
    await listener.start(handle)
    logger.info(
        "pam_integration started socket=%s mode=%s auto_session=%s",
        pam_cfg.notify_socket,
        pam_cfg.mode,
        pam_cfg.auto_session,
    )
    try:
        await asyncio.get_event_loop().create_future()  # wait until cancelled
    except asyncio.CancelledError:
        # Cancellation is the normal shutdown path; the finally block stops the listener.
        pass
    finally:
        await listener.stop()
        logger.info("pam_integration stopped")


async def _on_open(
    event: PamEvent,
    pam_cfg: PamConfig,
    registry: SessionRegistry,
    bridges: dict[str, object] | None = None,
    instances: dict[tuple[str, int], str] | None = None,
) -> None:
    """Create the login's session, then relay the event.

    The id is made here, once, and kept in *instances* for :func:`_on_close`:
    it carries the process's start time, which the close may no longer be able
    to read, and, failing that, a random tag no second computation reproduces.
    """
    from provide.uterm.server.pam_tunnel import PamTunnelBridge

    ev = event
    cfg = pam_cfg
    session_id = _session_id(ev)
    if instances is not None:
        instances[_login_key(ev)] = session_id

    if cfg.mode == "capture" and ev.capture_socket:
        await _create_capture_session(ev, cfg, registry, session_id)
    elif cfg.auto_session:
        await _create_notify_session(ev, cfg, registry, session_id)

    display_name = f"{ev.username} ({ev.tty or 'pam'})"

    if cfg.relay_url and cfg.relay_token:
        await _forward_to_relay(
            {
                "event": "open",
                "username": ev.username,
                "tty": ev.tty,
                "pid": ev.pid,
                "mode": ev.mode,
            },
            cfg.relay_url,
            cfg.relay_token,
        )
        result = await _create_relay_tunnel(cfg.relay_url, cfg.relay_token, session_id, display_name)
        if result is not None and bridges is not None:
            worker_token, ws_endpoint = result
            connector = _get_connector(registry, session_id)
            if connector is not None:
                bridge = PamTunnelBridge(ws_endpoint, worker_token, connector)
                try:
                    await bridge.start()
                    bridges[session_id] = bridge
                except Exception as exc:
                    logger.warning("pam_tunnel_start_failed session_id=%s error=%s", session_id, exc)
                    with contextlib.suppress(Exception):
                        await bridge.stop()


async def _on_close(
    event: PamEvent,
    pam_cfg: PamConfig,
    registry: SessionRegistry,
    bridges: dict[str, object] | None = None,
    instances: dict[tuple[str, int], str] | None = None,
) -> None:
    ev = event
    cfg = pam_cfg
    # The id the open gave this login. Without one (the open came before a
    # restart) it is computed afresh, which finds the session while the
    # process that ran PAM is still alive to report its start time.
    known = instances.pop(_login_key(ev), None) if instances is not None else None
    session_id = known if known is not None else _session_id(ev)

    # Stop tunnel bridge first
    bridge = bridges.pop(session_id, None) if bridges is not None else None
    if bridge is not None:
        try:
            stop = getattr(bridge, "stop", None)
            if callable(stop):  # pragma: no branch
                await stop()
        except Exception as exc:
            logger.debug("pam_bridge_stop_failed session_id=%s error=%s", session_id, exc)

    if cfg.relay_url and cfg.relay_token:
        await _forward_to_relay(
            {"event": "close", "username": ev.username, "tty": ev.tty, "pid": ev.pid},
            cfg.relay_url,
            cfg.relay_token,
        )

    try:
        await registry.delete_session(session_id)
        logger.info("pam_session_deleted session_id=%s", session_id)
    except Exception as exc:
        logger.debug("pam_session_delete_failed session_id=%s error=%s", session_id, exc)


# ── notify mode ───────────────────────────────────────────────────────────────


async def _create_notify_session(
    event: PamEvent, pam_cfg: PamConfig, registry: SessionRegistry, session_id: str | None = None
) -> None:
    ev = event
    cfg = pam_cfg

    session_id = session_id if session_id is not None else _session_id(ev)
    command = cfg.auto_session_command or "/bin/bash"

    payload: dict[str, object] = {
        "session_id": session_id,
        "display_name": f"{ev.username} ({ev.tty or 'pam'})",
        "connector_type": "pty",
        "connector_config": {
            "command": command,
            "username": ev.username,
            "inject": False,
        },
        "input_mode": "hijack",
        "auto_start": True,
        # A PAM login owns its session: it leaves on PAM close, not when the last viewer leaves.
        "ephemeral": False,
        "tags": ["pam", "notify", ev.username],
        "visibility": "operator",
    }
    await _safe_create(registry, payload)


# ── capture mode ──────────────────────────────────────────────────────────────


async def _create_capture_session(
    event: PamEvent, pam_cfg: PamConfig, registry: SessionRegistry, session_id: str | None = None
) -> None:
    ev = event
    if ev.capture_socket is None:
        return

    # ── capture_socket path confinement ──────────────────────────────────────
    # Determine the trusted base directory for capture sockets.
    # Priority: explicit capture_socket_dir > parent of notify_socket > no confinement.
    base_dir: str | None = None
    if pam_cfg.capture_socket_dir:
        base_dir = pam_cfg.capture_socket_dir
    elif pam_cfg.notify_socket:
        from pathlib import Path as _Path

        base_dir = str(_Path(pam_cfg.notify_socket).parent)

    if base_dir is not None:
        from pathlib import Path as _Path

        try:
            resolved = _Path(ev.capture_socket).resolve()
            trusted = _Path(base_dir).resolve()
            # is_relative_to is a proper path-segment containment check (true
            # for an exact match too), unlike a string-prefix compare — which
            # both false-matches /run/evil against /run/uterm and assumes "/"
            # as the separator, breaking on Windows.
            if not resolved.is_relative_to(trusted):
                logger.warning(
                    "pam_capture_socket_confined socket=%r is outside trusted dir=%r — session NOT created",
                    ev.capture_socket,
                    base_dir,
                )
                return
        except Exception:
            logger.warning(
                "pam_capture_socket_confined failed to resolve socket=%r — session NOT created",
                ev.capture_socket,
            )
            return

    session_id = session_id if session_id is not None else _session_id(ev)

    payload: dict[str, object] = {
        "session_id": session_id,
        "display_name": f"{ev.username} ({ev.tty or 'pam'}) [live]",
        "connector_type": "pty_capture",
        "connector_config": {
            "socket_path": ev.capture_socket,
        },
        "input_mode": "open",
        "auto_start": True,
        # A PAM login owns its session: it leaves on PAM close, not when the last viewer leaves.
        "ephemeral": False,
        "tags": ["pam", "capture", ev.username],
        "visibility": "operator",
    }
    await _safe_create(registry, payload)


# ── helpers ───────────────────────────────────────────────────────────────────


async def _safe_create(registry: SessionRegistry, payload: dict[str, object]) -> None:
    session_id = str(payload.get("session_id", ""))
    try:
        await registry.create_session(payload)
        logger.info("pam_session_created session_id=%s", session_id)
    except Exception as exc:
        logger.warning("pam_session_create_failed session_id=%s error=%s", session_id, exc)


def _get_runtime(registry: SessionRegistry, session_id: str) -> object | None:
    """Return the HostedSessionRuntime if present, else None."""
    return registry.get_runtime(session_id)


def _get_connector(registry: SessionRegistry, session_id: str) -> object | None:
    """Return the connector for a session if present, else None."""
    try:
        runtime = _get_runtime(registry, session_id)
        if runtime is None:
            return None
        return getattr(runtime, "connector", None)
    except Exception:
        return None
