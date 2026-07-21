#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RFB upstream dial for the human VNC WebSocket relay.

Resolves a seeded/runtime :class:`GraphicalTargetDefinition` (protocol ``rfb``)
to a duplex binary stream pair for :func:`run_human_relay_streams`.

TLS is opt-in via target ``config`` keys (or ``rfbs://`` endpoint form before
normalization):

* ``tls`` / ``ssl`` (bool) — wrap the TCP socket with TLS (default False).
* ``tls_insecure`` / ``ssl_insecure`` (bool) — skip cert verify + hostname
  check (lab self-signed only). Default **False** (fail-closed verify-on).

Dial failures and non-RFB / missing targets return ``None`` so the WS route
closes with 1013 ``upstream unavailable``.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

from provide.telemetry import get_logger
from provide.uterm.server.graphical_targets import (
    PROTOCOL_RFB,
    GraphicalTargetDefinition,
    InMemoryGraphicalTargetRegistry,
    parse_rfb_endpoint,
    system_scope,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.server.bridge.routes.ws_gui_vnc import UpstreamDuplexFactory

logger = get_logger(__name__)

# Default TCP connect timeout (seconds). Lab and production RFB peers should
# answer quickly; keep this short so a dead target fails closed promptly.
DEFAULT_CONNECT_TIMEOUT_S = 5.0

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class RfbDialConfig:
    """Resolved plain/TLS dial parameters for one RFB target."""

    host: str
    port: int
    tls: bool = False
    tls_insecure: bool = False
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    target_id: str = ""

    def endpoint_label(self) -> str:
        scheme = "rfbs" if self.tls else "rfb"
        return f"{scheme}://{self.host}:{self.port}"


def _as_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return default


def _config_map(target: GraphicalTargetDefinition) -> dict[str, object]:
    raw = target.config if isinstance(target.config, dict) else {}
    return {str(k): v for k, v in raw.items()}


def dial_config_from_target(target: GraphicalTargetDefinition) -> RfbDialConfig | None:
    """Build :class:`RfbDialConfig` from a graphical target, or ``None`` if not RFB.

    Endpoint may still be the pre-validate form ``rfbs://host:port``; after
    :meth:`GraphicalTargetDefinition.validate` it is normalized to ``host:port``
    and TLS flags live only in ``config``.
    """
    protocol = (target.protocol or "").strip().lower()
    if protocol != PROTOCOL_RFB:
        return None

    cfg = _config_map(target)
    endpoint_raw = (target.endpoint or "").strip()
    tls_from_scheme = endpoint_raw.lower().startswith("rfbs://")
    # parse_rfb_endpoint only accepts rfb:// or host:port — normalize rfbs first.
    parse_endpoint = endpoint_raw
    if tls_from_scheme:
        parse_endpoint = "rfb://" + endpoint_raw[len("rfbs://") :]
    host, port = parse_rfb_endpoint(parse_endpoint if parse_endpoint else None)

    tls = tls_from_scheme or _as_bool(cfg.get("tls"), default=False) or _as_bool(cfg.get("ssl"), default=False)
    tls_insecure = _as_bool(cfg.get("tls_insecure"), default=False) or _as_bool(cfg.get("ssl_insecure"), default=False)
    timeout_raw = cfg.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S)
    try:
        timeout_s = float(timeout_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timeout_s = DEFAULT_CONNECT_TIMEOUT_S
    if timeout_s <= 0:
        timeout_s = DEFAULT_CONNECT_TIMEOUT_S

    return RfbDialConfig(
        host=host,
        port=port,
        tls=tls,
        tls_insecure=tls_insecure,
        connect_timeout_s=timeout_s,
        target_id=target.target_id,
    )


def _ssl_context(*, tls_insecure: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_default_certs()
    return ctx


def open_rfb_upstream(
    dial: RfbDialConfig,
    *,
    create_connection: Callable[..., socket.socket] | None = None,
) -> tuple[BinaryIO, BinaryIO]:
    """TCP(+TLS) dial *dial* and return ``(makefile_rb, makefile_wb)``.

    *create_connection* is injectable for unit tests (signature of
    :func:`socket.create_connection`). The returned streams share the same
    underlying socket; closing either is enough (caller should close both).
    """
    connect = create_connection or socket.create_connection
    raw = connect((dial.host, dial.port), dial.connect_timeout_s)
    sock: socket.socket | ssl.SSLSocket = raw
    try:
        if dial.tls:
            ctx = _ssl_context(tls_insecure=dial.tls_insecure)
            # server_hostname required for SNI / verify when hostname is a name;
            # for IP + insecure lab mode, pass host anyway (check_hostname off).
            sock = ctx.wrap_socket(raw, server_hostname=dial.host)
            raw = None  # ownership transferred
        sock.settimeout(None)
        # Unbuffered binary makefiles: RFB peers send ProtocolVersion then wait
        # (no EOF). Default buffered makefile("rb") can block a large read until
        # the buffer fills, stalling the human relay until the peer closes.
        # makefile has no closefd=; close both stream ends in the relay finally.
        upstream_r = sock.makefile("rb", buffering=0)
        upstream_w = sock.makefile("wb", buffering=0)
        # makefile() keeps a ref on the socket object.
        return upstream_r, upstream_w
    except Exception:
        if raw is not None:
            raw.close()
        else:
            sock.close()
        raise


def resolve_rfb_target(
    registry: InMemoryGraphicalTargetRegistry,
    target_id: str | None,
) -> GraphicalTargetDefinition | None:
    """Look up *target_id* under system scope (seeded static + runtime)."""
    if not target_id or not str(target_id).strip():
        return None
    tid = str(target_id).strip()
    try:
        return registry.get(system_scope(), tid)
    except Exception as exc:
        logger.debug("vnc_target_lookup_error target_id=%s error=%s", tid, exc)
        return None


def make_vnc_upstream_factory(
    registry: InMemoryGraphicalTargetRegistry,
    *,
    create_connection: Callable[..., socket.socket] | None = None,
) -> UpstreamDuplexFactory:
    """Return a ``hub.vnc_upstream_factory`` that dials RFB targets from *registry*."""

    def factory(_worker_id: str, target_id: str | None) -> tuple[BinaryIO, BinaryIO] | None:
        target = resolve_rfb_target(registry, target_id)
        if target is None:
            logger.info("vnc_upstream_missing_target target_id=%s", target_id)
            return None
        dial = dial_config_from_target(target)
        if dial is None:
            logger.info(
                "vnc_upstream_not_rfb target_id=%s protocol=%s",
                target.target_id,
                target.protocol,
            )
            return None
        try:
            streams = open_rfb_upstream(dial, create_connection=create_connection)
        except OSError as exc:
            logger.warning(
                "vnc_upstream_dial_failed target_id=%s endpoint=%s error=%s",
                dial.target_id,
                dial.endpoint_label(),
                exc,
            )
            return None
        except ssl.SSLError as exc:
            logger.warning(
                "vnc_upstream_tls_failed target_id=%s endpoint=%s error=%s",
                dial.target_id,
                dial.endpoint_label(),
                exc,
            )
            return None
        logger.info(
            "vnc_upstream_dial_ok target_id=%s endpoint=%s tls=%s insecure=%s",
            dial.target_id,
            dial.endpoint_label(),
            dial.tls,
            dial.tls_insecure,
        )
        return streams

    return factory


def attach_vnc_upstream_factory(hub: Any, registry: InMemoryGraphicalTargetRegistry) -> None:
    """Set ``hub.vnc_upstream_factory`` from *registry* (idempotent overwrite)."""
    hub.vnc_upstream_factory = make_vnc_upstream_factory(registry)
