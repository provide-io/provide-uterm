#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SSH server helpers + process handler — split from :mod:`_gateway`.

Hosts the asyncssh ``SSHServer`` factory that accepts any connection
(password or pubkey), and the ``process_factory`` coroutine that bridges
each accepted SSH session to the upstream WebSocket terminal server.
Kept in a dedicated module so :mod:`_gateway` stays under 500 LOC.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import ssl as _ssl
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    import asyncssh

    from provide.uterm.auth import SSHKeyResolver
    from provide.uterm.colors import ColorMode

from provide.telemetry import get_logger
from provide.uterm.auth import ResolvedIdentity
from provide.uterm.control_channel import encode_control_frame
from provide.uterm.control_channel_builders import make_identity
from provide.uterm.gateway._gateway import (
    _read_token,
    _run_gateway_session,
    _ssh_to_ws,
    _ws_to_ssh,
)
from provide.uterm.gateway._iac_negotiate import derive_colormode

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# SSH server helpers (module-level for testability)
# ---------------------------------------------------------------------------


def _make_no_auth_server_class(
    key_resolver: SSHKeyResolver | None = None,
    *,
    require_resolver: bool = False,
) -> type:
    """Return an asyncssh.SSHServer subclass that accepts inbound connections.

    Args:
        key_resolver: Optional :class:`SSHKeyResolver`. When provided, its
            ``resolve`` method is called during public-key auth. A non-None
            return stashes the :class:`ResolvedIdentity` on the server
            instance so :func:`_make_process_handler` can forward it via
            an ``identity`` control-frame to the upstream WebSocket.
        require_resolver: When True, public-key auth fails if the resolver
            returns None. Typically paired with disabling password auth at
            the SSH server level. Defaults to False — unknown keys fall
            through to password auth, preserving historical behaviour.

    Pubkey auth is accepted when offered (any key is allowed if no resolver
    is configured), so the connection's authenticated public key is
    available via ``conn.get_extra_info('public_key')`` for identity-aware
    features like per-fingerprint token paths. Password auth is also
    allowed so plain SSH clients without a key still work. Do NOT bind
    host="0.0.0.0" on a public interface without an external firewall or
    auth layer.
    """
    import asyncssh

    class _NoAuthServer(asyncssh.SSHServer):
        def __init__(self) -> None:
            super().__init__()
            # Captured during validate_public_key; read by the process
            # handler to route per-fingerprint token paths.
            self._accepted_client_key: object | None = None
            self._conn: object | None = None
            # Populated by the resolver (when configured) — forwarded to
            # the upstream server as an ``identity`` control frame.
            self._resolved_identity: ResolvedIdentity | None = None

        def connection_made(self, conn: object) -> None:
            self._conn = conn
            # Stash the server on the connection so the process handler
            # can recover the accepted pubkey — asyncssh exposes no
            # first-class 'public_key' via get_extra_info.
            with contextlib.suppress(Exception):
                setattr(conn, "_warp_gateway_server", self)  # noqa: B010

        def begin_auth(self, username: str) -> bool:
            # Require auth so asyncssh actually exercises the pubkey
            # handler below. Callers still get no-gate behaviour because
            # we accept everything (unless require_resolver is set).
            return True

        def public_key_auth_supported(self) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            # When the resolver is mandatory, disable password fallback —
            # only resolver-accepted keys should auth.
            return not require_resolver

        async def validate_public_key(self, username: str, key: object) -> bool:
            self._accepted_client_key = key
            fp = _fingerprint_for_key(key) or ""
            logger.debug(
                "ssh_pubkey_accepted",
                key_type=type(key).__name__,
                fp=fp,
            )
            if key_resolver is None:
                return True
            pubkey_blob = _openssh_blob_for_key(key)
            identity = await key_resolver.resolve(
                fp,
                pubkey_blob=pubkey_blob,
                username=username,
            )
            if identity is None:
                if require_resolver:
                    logger.info("ssh_pubkey_rejected_by_resolver", fp=fp, user=username)
                    return False
                logger.debug("ssh_pubkey_unknown_fallthrough", fp=fp, user=username)
                return True  # Unknown — fall through to other auth.
            # Always overwrite with the actual client-key fingerprint,
            # regardless of what the resolver returned. A misbehaving or
            # buggy resolver could otherwise forward a forged fingerprint
            # downstream and make audit logs lie about which key was
            # actually presented. The resolver is authoritative for
            # *subject* and *claims*; the gateway is authoritative for
            # the fingerprint.
            identity = ResolvedIdentity(
                subject=identity.subject,
                claims=identity.claims,
                fingerprint=fp,
            )
            self._resolved_identity = identity
            logger.info(
                "ssh_pubkey_resolved",
                subject=identity.subject,
                fp=fp,
                user=username,
            )
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return True

        def kbdint_auth_supported(self) -> bool:
            # Enable keyboard-interactive so clients that fall through
            # from pubkey/password (e.g. ``ssh -o PreferredAuthentications=keyboard-interactive``)
            # still land. We accept the first response unconditionally.
            # Disabled when require_resolver is set — otherwise an unknown
            # key followed by kbdint would bypass the resolver gate.
            return not require_resolver

        def get_kbdint_challenge(self, username: str, lang: str, submethods: str) -> tuple[str, str, str, list[Any]]:
            # Issue an empty challenge (no prompts): asyncssh then calls
            # validate_kbdint_response with an empty response list, which we
            # accept. Without these two overrides, advertising kbdint via
            # kbdint_auth_supported() left the method failing silently — the base
            # SSHServer returns no challenge, so the client's kbdint attempt was
            # rejected. kbdint is only offered when not require_resolver, so
            # accepting here matches the no-gate posture of the other handlers.
            return ("", "", "", [])

        def validate_kbdint_response(self, username: str, responses: Any) -> bool:
            return True

    return _NoAuthServer


def _openssh_blob_for_key(key: object) -> bytes:
    """Best-effort extraction of an OpenSSH public-key blob from an asyncssh key.

    Prefers the binary wire format via ``key.export_public_key()``. Falls
    back to empty bytes if the key object doesn't expose a known method —
    resolvers that key off fingerprint alone still work.
    """
    if key is None:
        return b""
    for attr in ("export_public_key", "public_data"):
        method = getattr(key, attr, None)
        if callable(method):
            try:
                result = method()
            except Exception:
                continue
            if isinstance(result, bytes):
                return result
            if isinstance(result, str):
                return result.encode("ascii", errors="replace")
    return b""


def _fingerprint_for_key(key: object) -> str | None:
    """Return an OpenSSH-style SHA256 fingerprint for an asyncssh key, or None."""
    if key is None:
        return None
    get_fp = getattr(key, "get_fingerprint", None)
    if not callable(get_fp):
        return None
    try:
        fp: str | None = get_fp("sha256")
        return fp
    except Exception:
        return None


def _token_file_for_connection(base: Path | None, fingerprint: str | None) -> Path | None:
    """Resolve the per-connection token file path.

    If ``base`` is a directory (or a path that does not exist and has no
    suffix), treat it as a per-user token dir and append a filename
    derived from the SSH client's pubkey fingerprint. Otherwise the
    file path is used verbatim.
    """
    if base is None:
        return None
    # Directory or a dir-shaped path → per-fingerprint file below it.
    is_dir_like = base.is_dir() or (not base.exists() and base.suffix == "")
    if is_dir_like and fingerprint:
        slug = fingerprint.replace(":", "_").replace("/", "_").replace("+", "-")
        return base / f"{slug}.token"
    return base


async def _ssh_pump(
    process: asyncssh.SSHServerProcess[Any],
    url: str,
    *,
    ws_ssl: _ssl.SSLContext | bool | None,
    token_holder: list[dict[str, Any] | None],
    color_mode: ColorMode,
    token_file: Path | None,
    resolved_identity: ResolvedIdentity | None,
    upstream_proxy_secret: str | bytes | None,
    redirect_holder: list[str | None] | None,
) -> int | None:
    """Run one WebSocket connection attempt for an SSH session.

    Returns the WS close code or None.
    """
    import websockets

    connect_kwargs: dict[str, object] = {}
    if ws_ssl is not None:
        connect_kwargs["ssl"] = ws_ssl
    async with websockets.connect(url, **connect_kwargs) as ws:  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]
        if resolved_identity is not None:
            identity_msg = make_identity(
                subject=resolved_identity.subject,
                claims=dict(resolved_identity.claims),
                fingerprint=resolved_identity.fingerprint,
                transport="ssh",
                secret=upstream_proxy_secret,
            )
            await ws.send(encode_control_frame(identity_msg))
        token_data = token_holder[0]
        if token_data:  # pragma: no cover — resume frame is sent only after a prior successful session
            resume_msg: dict[str, object] = {"type": "resume", "token": token_data["token"]}
            if "player_id" in token_data:
                resume_msg["player_id"] = token_data["player_id"]
            await ws.send(encode_control_frame(resume_msg))
        # Advertise the gateway's own redirect-follow capability (mirrors _pipe_ws):
        # a redirect-aware server hands off via a `redirect` frame this gateway
        # follows, instead of proxying. Generic transport capability — no app/game
        # feature names here.
        await ws.send(encode_control_frame({"type": "hello", "v": 1, "features": ["supports_redirect"]}))
        t1 = asyncio.create_task(_ssh_to_ws(process, ws))
        t2 = asyncio.create_task(
            _ws_to_ssh(
                ws,
                process,
                token_holder=token_holder,
                color_mode=color_mode,
                token_file=token_file,
                redirect_holder=redirect_holder,
            )
        )
        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:  # pragma: no branch — may be empty if both finish
            task.cancel()
        await asyncio.gather(*[*_done, *pending], return_exceptions=True)
    return getattr(ws, "close_code", None)


async def _make_process_handler(
    ws_url: str,
    color_mode: ColorMode,
    token_file: Path | None = None,
    ws_ssl: _ssl.SSLContext | bool | None = None,
    upstream_proxy_secret: str | bytes | None = None,
) -> Callable[[asyncssh.SSHServerProcess[Any]], Coroutine[Any, Any, None]]:
    """Return an asyncssh process_factory coroutine bound to ws_url/color_mode.

    ``ws_ssl`` (optional) is forwarded to :func:`websockets.connect` when the
    upstream URL is ``wss://``. ``None`` defers to the default SSL context
    (CA-signed certs work). ``False`` disables cert verification (dev only).
    An :class:`ssl.SSLContext` instance is passed through verbatim — the
    normal way to trust an internal CA or self-signed cert.
    """

    async def _process_handler(process: asyncssh.SSHServerProcess[Any]) -> None:
        max_reconnects = 12
        reconnect_delay = 3.0
        stdin = process.stdin
        stdout = process.stdout

        # Resolve this connection's pubkey fingerprint (if any) so multi-user
        # SSH proxies can keep per-user token files instead of fighting over
        # one shared file. asyncssh exposes several paths to the SSHServer
        # instance from an SSHServerProcess; try each in turn.
        client_key = None
        resolved_identity: ResolvedIdentity | None = None
        candidates: list[object] = []
        for attr_chain in ("_chan._conn", "_chan.get_connection", "channel._conn", "channel.get_connection"):
            obj: object = process
            parts = attr_chain.split(".")
            with contextlib.suppress(AttributeError, Exception):
                for part in parts:
                    obj = getattr(obj, part)
                if callable(obj):
                    obj = cast("Callable[[], object]", obj)()
                candidates.append(obj)
        for (
            conn
        ) in candidates:  # pragma: no cover — asyncssh-server attribute fallbacks exercised via live-SSH integration
            if conn is None:
                continue
            server = getattr(conn, "_warp_gateway_server", None)
            if server is None:
                # asyncssh also keeps a reference to the Server instance
                # as conn._owner or conn._server; try both.
                for sattr in ("_owner", "_server"):
                    candidate = getattr(conn, sattr, None)
                    if candidate is not None and hasattr(candidate, "_accepted_client_key"):
                        server = candidate
                        break
            if server is not None:
                client_key = getattr(server, "_accepted_client_key", None)
                if resolved_identity is None:
                    resolved_identity = getattr(server, "_resolved_identity", None)
                if client_key is not None or resolved_identity is not None:
                    break
        fingerprint = _fingerprint_for_key(client_key)
        effective_token_file = _token_file_for_connection(token_file, fingerprint)
        # debug — no-op unless PROVIDE_LOG_LEVEL=DEBUG; important when
        # diagnosing "why did my pubkey not route to a per-user token".
        logger.debug(
            "ssh_session_start",
            fingerprint=fingerprint,
            has_client_key=client_key is not None,
            token_file=str(effective_token_file) if effective_token_file else None,
        )

        # Colour-palette hint: SSH has native protocol carriers for the
        # same info telnet negotiates via IAC — ``pty-req`` carries TERM
        # and ``env`` channel requests carry env vars like COLORTERM.
        # asyncssh surfaces both on the SSHServerProcess, so no
        # handshake is needed — just read, derive, append to ws_url.
        ssh_term: str | None = None
        ssh_env: dict[str, str] = {}
        with contextlib.suppress(AttributeError, Exception):
            get_ttype = getattr(process, "get_terminal_type", None)
            if callable(get_ttype):  # pragma: no branch — asyncssh always exposes get_terminal_type; defensive
                ssh_term = get_ttype()
        with contextlib.suppress(AttributeError, Exception):
            get_env = getattr(process, "get_environment", None)
            if callable(get_env):
                ssh_env = dict(get_env() or {})
        effective_ws_url = ws_url
        derived = derive_colormode(ssh_term, ssh_env)
        if derived:
            sep = "&" if "?" in effective_ws_url else "?"
            effective_ws_url = f"{effective_ws_url}{sep}colormode={derived}"
            logger.info(
                "ssh_colormode_negotiated",
                derived=derived,
                term=ssh_term or "",
                colorterm=ssh_env.get("COLORTERM", ""),
            )

        # Per-connection token-holder, optionally seeded from disk when the
        # caller opted in via ``token_file``. Discarded when this coroutine
        # returns; file persists across proxy restarts.
        token_holder: list[dict[str, Any] | None] = [None]
        if (
            effective_token_file is not None
        ):  # pragma: no cover — saved-token load is reachable only inside a live SSH session
            saved = _read_token(effective_token_file)
            if saved:
                token_holder[0] = saved

        redirect_holder: list[str | None] = [None]

        async def pump(url: str) -> int | None:
            return await _ssh_pump(
                process,
                url,
                ws_ssl=ws_ssl,
                token_holder=token_holder,
                color_mode=color_mode,
                token_file=effective_token_file,
                resolved_identity=resolved_identity,
                upstream_proxy_secret=upstream_proxy_secret,
                redirect_holder=redirect_holder,
            )

        async def show_reconnecting() -> None:
            with contextlib.suppress(Exception):
                stdout.write("\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8")

        try:
            await _run_gateway_session(
                ws_url=effective_ws_url,
                redirect_holder=redirect_holder,
                pump=pump,
                client_connected=lambda: not (hasattr(stdin, "at_eof") and stdin.at_eof()),
                show_reconnecting=show_reconnecting,
                max_reconnects=max_reconnects,
                reconnect_delay=reconnect_delay,
            )
        except Exception as exc:
            logger.debug("ssh_ws_session_ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                process.exit(0)

    return _process_handler
