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
  close is handled the process may already be gone. The memory is bounded: on
  every open and close, entries whose process has provably ended (its pid now
  reports another start time) for at least 60 s are dropped -- the grace lets a
  close that is already on its way find its entry -- and past 4096 entries the
  oldest go.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.pty.pam_listener import PamEvent
    from provide.uterm.server.models import PamConfig, ServerConfig
    from provide.uterm.server.registry import SessionRegistry

logger = logging.getLogger(__name__)

# Fire-and-forget tasks (the capture-owner hook call). A task that loses all
# references can be GC'd before it completes, so each is held here until it
# finishes, then discarded. See _resolve_owner_via_hook.
_hook_tasks: set[asyncio.Task[None]] = set()

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


def pam_login_alive(pid: int, start_ticks: int) -> bool:
    """Whether the process a PAM capture id names still runs: its pid reports the start ticks the id carries.

    A pid since reused by another login reports another start, so it does not
    pass. The restore of a persisted capture session asks this before it binds
    anything.
    """
    return _process_start_ticks(pid) == start_ticks


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


# The most logins remembered between open and close. Each open login holds one
# entry, so this is far above any real concurrency; it only bounds the map when
# closes go missing on a host where start times cannot be read (see
# _prune_logins).
_MAX_REMEMBERED_LOGINS = 4096


def _login_is_gone(key: tuple[str, int], session_id: str) -> bool:
    """True when the process an entry's id names has provably ended.

    Only an id that carries a start time can say: the login is gone when its pid
    now reports another start time, or none. A tty-named id or a random tag
    proves nothing, and is kept until its close or the cap.
    """
    match = re.search(rf"-{key[1]}-([0-9]+)\Z", session_id)
    return match is not None and str(_process_start_ticks(key[1])) != match.group(1)


# How long an entry must have looked gone before it is dropped. pam_uterm.so
# sends the close and the PAM process exits straight after, so a login is
# "gone" by its start time before its own close has been handled; dropping it
# then would leave the close to recompute an id it can no longer reproduce
# (a random tag), and the capture session would never be deleted.
_GONE_GRACE_S = 60.0

# The clock the grace is measured on; swappable in tests.
_monotonic = time.monotonic


class OpenLogins(dict[tuple[str, int], str]):
    """The id each open login was given, plus when an entry was first seen gone."""

    def __init__(self) -> None:
        super().__init__()
        self.gone_since: dict[tuple[str, int], float] = {}


def _prune_logins(instances: dict[tuple[str, int], str], keep: tuple[str, int] | None = None) -> None:
    """Forget logins whose close never came, so the map stays bounded.

    An entry whose process has provably ended (:func:`_login_is_gone`) is
    dropped once it has looked gone for :data:`_GONE_GRACE_S`, which leaves its
    own close, already on its way, time to find it. Past
    :data:`_MAX_REMEMBERED_LOGINS` the oldest go regardless. *keep* is never
    dropped. A plain dict (no :class:`OpenLogins` bookkeeping) is only capped.
    """
    gone_since = getattr(instances, "gone_since", None)
    if gone_since is not None:
        now = _monotonic()
        for key in list(gone_since):
            if key not in instances:
                del gone_since[key]
        for key, session_id in list(instances.items()):
            if key == keep or not _login_is_gone(key, session_id):
                gone_since.pop(key, None)
                continue
            if now - gone_since.setdefault(key, now) >= _GONE_GRACE_S:
                del instances[key]
                del gone_since[key]
    for key in list(instances):
        if len(instances) <= _MAX_REMEMBERED_LOGINS:
            break
        if key != keep:
            del instances[key]
            if gone_since is not None:
                gone_since.pop(key, None)


def _remember_login(instances: dict[tuple[str, int], str], key: tuple[str, int], session_id: str) -> None:
    """Remember the id an opening login was given, for its close to find."""
    instances.pop(key, None)  # re-inserted last: the newest entry is the last the cap drops
    instances[key] = session_id
    _prune_logins(instances, keep=key)


async def run_pam_integration(
    config: ServerConfig,
    registry: SessionRegistry,
    on_metric: Callable[[str, int], None] | None = None,
) -> None:
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
    _instances = OpenLogins()

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
            await _on_open(ev, pam_cfg, registry, _bridges, _instances, on_metric=on_metric)
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
    *,
    on_metric: Callable[[str, int], None] | None = None,
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
        _remember_login(instances, _login_key(ev), session_id)

    if cfg.mode == "capture" and ev.capture_socket:
        await _create_capture_session(ev, cfg, registry, session_id, on_metric=on_metric)
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
    if instances is not None:
        _prune_logins(instances)

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
    event: PamEvent,
    pam_cfg: PamConfig,
    registry: SessionRegistry,
    session_id: str | None = None,
    *,
    on_metric: Callable[[str, int], None] | None = None,
) -> None:
    ev = event
    if ev.capture_socket is None:
        return

    if not capture_socket_allowed(ev.capture_socket, pam_cfg):
        return

    session_id = session_id if session_id is not None else _session_id(ev)

    # ── owner assignment (Decision 1) ───────────────────────────────────────
    # capture_owner selects how a capture session's owner is set when it is
    # created. "pam_user" (default): the owner is the PAM login name, verbatim.
    # "hook": uterm asks a configured URL who owns the login. "none": today's
    # behaviour, no owner. The session is always created first (its capture
    # socket is bound); the hook runs as a task beside it, never blocking the
    # login. On failure the session stays ownerless — the existing admin routes
    # (PATCH /api/sessions/{id}, PATCH /api/previous-sessions/{id}) are the
    # fallback, as they are the whole mechanism today.
    owner: str | None = None
    if pam_cfg.capture_owner == "pam_user":
        owner = ev.username

    payload: dict[str, object] = {
        "session_id": session_id,
        "display_name": f"{ev.username} ({ev.tty or 'pam'}) [live]",
        "connector_type": "pty_capture",
        "connector_config": {
            "socket_path": ev.capture_socket,
        },
        # hijack, not open: when a keystroke path arrives (0x06 from a
        # supervisor, or stdin_socket_path PATCHed by an admin), hijack is
        # what gives a single viewer the lease — open would open it to every
        # viewer at once. With no input path both are inert. (Decision 2.)
        "input_mode": "hijack",
        "auto_start": True,
        # A PAM login owns its session: it leaves on PAM close, not when the last viewer leaves.
        "ephemeral": False,
        "tags": ["pam", "capture", ev.username],
        "visibility": "operator",
    }
    if owner is not None:
        payload["owner"] = owner
    await _safe_create(registry, payload)

    # ── hook (capture_owner = "hook") ────────────────────────────────────────
    if pam_cfg.capture_owner == "hook":
        task = asyncio.create_task(_resolve_owner_via_hook(ev, pam_cfg, registry, session_id, on_metric=on_metric))
        _hook_tasks.add(task)
        task.add_done_callback(_hook_tasks.discard)


# ── hook (capture_owner = "hook") ────────────────────────────────────────────


async def _resolve_owner_via_hook(
    ev: PamEvent,
    pam_cfg: PamConfig,
    registry: SessionRegistry,
    session_id: str,
    *,
    on_metric: Callable[[str, int], None] | None = None,
) -> None:
    """Ask the configured hook who owns a capture session's login.

    Runs as a fire-and-forget task beside the session. The session is already
    created (ownerless) by the time this runs; on success the owner and
    visibility are patched in-place. On failure the session stays ownerless
    and the existing admin routes are the fallback.

    Failures are counted via ``on_metric`` (``pam_capture_owner_hook_failed_total``)
    and logged at warning: ``pam_capture_owner_hook_failed session_id=...
    reason=<one word>``. The reason never quotes the response body.
    """
    from provide.uterm.server.pam_owner_hook import (
        HookError,
        call_capture_owner_hook,
        fingerprint_ssh_keys,
    )

    ssh_keys = fingerprint_ssh_keys(ev.ssh_auth_info)
    try:
        answer = await call_capture_owner_hook(
            pam_cfg,
            session_id=session_id,
            username=ev.username,
            tty=ev.tty or None,
            pid=ev.pid,
            start_ticks=ev.start_ticks,
            ssh_keys=ssh_keys,
        )
    except HookError as exc:
        logger.warning(
            "pam_capture_owner_hook_failed session_id=%s reason=%s",
            session_id,
            exc.reason,
        )
        if on_metric is not None:
            on_metric("pam_capture_owner_hook_failed_total", 1)
        return
    except Exception as exc:
        # An unexpected error (not a HookError) — log it but don't crash the
        # task. The session stays ownerless.
        logger.warning(
            "pam_capture_owner_hook_failed session_id=%s reason=unexpected error=%s",
            session_id,
            exc,
        )
        if on_metric is not None:
            on_metric("pam_capture_owner_hook_failed_total", 1)
        return

    # The hook answered. Patch the session's owner and visibility in-place.
    patch: dict[str, object] = {}
    if answer.owner is not None:
        patch["owner"] = answer.owner
    if answer.visibility is not None:
        patch["visibility"] = answer.visibility
    if not patch:
        # A null owner and null visibility is a deliberate "no owner" answer;
        # nothing to patch.
        return
    try:
        await registry.update_session(session_id, patch, allow_owner_change=True)
        logger.info(
            "pam_capture_owner_hook_resolved session_id=%s owner=%s",
            session_id,
            answer.owner,
        )
        if on_metric is not None:
            on_metric("pam_capture_owner_hook_success_total", 1)
    except Exception as exc:
        # The session may have already closed (the login ended within the
        # hook's timeout window). PATCH /api/previous-sessions/{id} is the
        # fallback for that case, but here we just log and move on.
        logger.warning(
            "pam_capture_owner_hook_patch_failed session_id=%s error=%s",
            session_id,
            exc,
        )
        if on_metric is not None:
            on_metric("pam_capture_owner_hook_failed_total", 1)


def capture_socket_allowed(capture_socket: str, pam_cfg: PamConfig) -> bool:
    """Whether a capture socket path lies inside the directory capture sockets are confined to.

    That is ``pam.capture_socket_dir``, else the notify socket's directory, else
    nowhere in particular (no confinement). Checked when PAM opens a capture
    session, and again when one is re-created from its file after a restart: a
    path read back from disk is trusted no further than one PAM sent.
    """
    base_dir: str | None = None
    if pam_cfg.capture_socket_dir:
        base_dir = pam_cfg.capture_socket_dir
    elif pam_cfg.notify_socket:
        base_dir = str(Path(pam_cfg.notify_socket).parent)

    if base_dir is None:
        return True
    try:
        resolved = Path(capture_socket).resolve()
        trusted = Path(base_dir).resolve()
        # is_relative_to is a proper path-segment containment check (true
        # for an exact match too), unlike a string-prefix compare — which
        # both false-matches /run/evil against /run/uterm and assumes "/"
        # as the separator, breaking on Windows.
        if not resolved.is_relative_to(trusted):
            logger.warning(
                "pam_capture_socket_confined socket=%r is outside trusted dir=%r — session NOT created",
                capture_socket,
                base_dir,
            )
            return False
    except Exception:
        logger.warning(
            "pam_capture_socket_confined failed to resolve socket=%r — session NOT created",
            capture_socket,
        )
        return False
    return True


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
