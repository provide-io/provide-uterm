#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Input-hardening validators and snapshot shaping for the MCP tool surface.

These helpers are the security chokepoints that every MCP tool funnels untrusted
(LLM/caller-supplied) input through before it reaches the server: host/SSRF
classification, ReDoS-guarded regex compilation, path-segment id validation,
connector-config vetting, and snapshot output shaping.  They live here, factored
out of :mod:`provide.uterm.ai.server_impl`, so the tool-registration modules stay
small while the validators remain individually unit-testable.

``server_impl`` re-exports the public names (``_clean_snapshot``, ``_trim_tail``,
``_validate_session_create_config``, ``_compile_user_pattern``) so existing
callers/tests importing from ``provide.uterm.ai.server_impl`` keep working.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from provide.uterm.screen import strip_ansi

from provide.uterm.ai.constants import (
    ALLOW_PRIVATE_HOSTS,
    MAX_USER_PATTERN_LEN,
)
from provide.uterm.ai.patterns import has_catastrophic_construct
from provide.uterm.ai.policy import is_allowed_connector
from provide.uterm.client.hijack import _safe_id

# Hostnames that resolve to cloud metadata / internal-only endpoints. We refuse
# these by name (no blocking DNS lookup) — DNS-rebinding / egress filtering
# remains the server's responsibility.
_BLOCKED_HOST_NAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
    },
)


def _numeric_ipv4(candidate: str) -> ipaddress.IPv4Address | None:
    """Resolve the non-canonical numeric IPv4 forms that :mod:`ipaddress` rejects
    but the C resolver (``inet_aton``, used by sockets / httpx / curl) accepts:
    decimal (``2130706433``), octal (``0177.0.0.1``), hex (``0x7f.1``), and the
    shortened ``127.1`` forms. A blocklist that only understands the canonical
    dotted-quad is trivially bypassed by these (``http://2130706433`` reaches
    127.0.0.1), so normalise and re-check. Returns ``None`` for anything
    ``inet_aton`` rejects — i.e. a real hostname, which the server's
    DNS-resolving egress guard then handles. ``inet_aton`` is purely numeric and
    never performs DNS.
    """
    import socket

    try:
        packed = socket.inet_aton(candidate)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def _is_internal_host(host: str) -> bool:
    """Return ``True`` when *host* targets an internal / metadata endpoint.

    Literal IPs are classified with :mod:`ipaddress` (loopback, link-local,
    and — unless :data:`ALLOW_PRIVATE_HOSTS` — private / unique-local ranges).
    Hostnames are matched against a small denylist of well-known metadata
    names. We never perform a DNS lookup here: rebinding and egress control are
    the server's responsibility.
    """
    # Strip a trailing root dot ("localhost." == "localhost") before matching,
    # otherwise the FQDN form slips past the exact-match denylist.
    candidate = host.strip().strip("[]").rstrip(".").lower()
    if candidate in _BLOCKED_HOST_NAMES:
        return True
    # RFC 6761: ``localhost`` and any ``*.localhost`` name is reserved and must
    # resolve to loopback, so treat the whole subtree as internal.
    if candidate.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        # Not a canonical IP string. It may still be a non-canonical numeric
        # IPv4 form (decimal / octal / hex / shortened) that resolvers accept —
        # normalise and re-check so the blocklist can't be bypassed by them. A
        # genuine hostname yields None here and is left to the server's egress
        # guard (which resolves DNS); we deliberately never resolve DNS here.
        numeric = _numeric_ipv4(candidate)
        if numeric is None:
            return False
        ip = numeric
    if ip.is_loopback or ip.is_link_local:
        return True
    return not ALLOW_PRIVATE_HOSTS and (ip.is_private or ip.is_reserved or ip.is_unspecified)


def _compile_user_pattern(pattern: str) -> re.Pattern[str]:
    """Compile an attacker-supplied regex behind a length + structural guard.

    Two ReDoS mitigations run before ``re.compile``:

    * a length cap (:data:`MAX_USER_PATTERN_LEN`) removes the cheap
      amplification path; and
    * a structural denylist (:func:`~provide.uterm.ai.patterns.has_catastrophic_construct`)
      rejects nested quantifiers (``(a+)+``) and quantified backreferences
      (``\\1+``) — the classic exponential-backtracking shapes that a short,
      under-the-cap pattern can still trigger.

    ``re2``/``regex`` (linear-time engines) are not project dependencies, so we
    do not bound matching time directly. Residual risk: the structural guard
    does not catch every ReDoS shape (e.g. overlapping alternations like
    ``(a|a)*``); see the :mod:`~provide.uterm.ai.patterns` module docstring.
    """
    if len(pattern) > MAX_USER_PATTERN_LEN:
        msg = f"pattern too long (max {MAX_USER_PATTERN_LEN} chars)"
        raise ValueError(msg)
    if has_catastrophic_construct(pattern):
        msg = "pattern rejected: catastrophic-backtracking construct (nested quantifier or quantified backreference)"
        raise ValueError(msg)
    try:
        return re.compile(pattern)
    except re.error as exc:
        msg = f"invalid pattern: {exc}"
        raise ValueError(msg) from exc


def _reject_bad_pattern(pattern: str | None) -> dict[str, Any] | None:
    """Validate a user-supplied regex, returning a rejection dict or ``None``.

    ``None`` pattern is allowed (no filter requested). A pattern that is too
    long or otherwise invalid yields a structured ``invalid_pattern`` error so
    an LLM cannot trigger ReDoS via an oversized/complex regex.
    """
    if pattern is None:
        return None
    try:
        _compile_user_pattern(pattern)
    except ValueError as exc:
        return {
            "success": False,
            "error": "invalid_pattern",
            "detail": str(exc),
        }
    return None


def _compiled_pattern_or_rejection(
    pattern: str | None,
) -> tuple[re.Pattern[str] | None, dict[str, Any] | None]:
    """Compile a user pattern once and return ``(compiled, rejection)``.

    ``(compiled, None)`` on success, ``(None, rejection)`` for a bad pattern,
    ``(None, None)`` when no pattern is requested. Lets a tool validate the
    pattern and reuse the compiled object without compiling it twice
    (validate-then-recompile).
    """
    if pattern is None:
        return None, None
    try:
        return _compile_user_pattern(pattern), None
    except ValueError as exc:
        return None, {"success": False, "error": "invalid_pattern", "detail": str(exc)}


def _reject_bad_id(value: str, kind: str = "id") -> dict[str, Any] | None:
    """Validate a caller/LLM-supplied path-segment id, returning a rejection
    dict or ``None``.

    Mirrors :func:`_reject_bad_pattern`: a bad id yields the structured
    ``{"success": False, "error": "invalid_id"}`` contract every other MCP
    validator uses, rather than letting ``_safe_id`` raise a ``ValueError``
    (which the framework surfaces as a ToolError). Same allow-list as
    ``_safe_id`` so the path-injection guarantee is unchanged.
    """
    try:
        _safe_id(value, kind)
    except ValueError as exc:
        return {
            "success": False,
            "error": "invalid_id",
            "detail": str(exc),
        }
    return None


def _reject_bad_ids(*pairs: tuple[str, str]) -> dict[str, Any] | None:
    """Validate several ``(value, kind)`` path-segment ids in order.

    Returns the first :func:`_reject_bad_id` rejection, or ``None`` when every
    id is valid. Lets a multi-id tool (e.g. ``hijack_*`` with both ``worker_id``
    and ``hijack_id``) emit the structured ``invalid_id`` contract for whichever
    id is bad instead of letting ``_safe_id`` raise a ToolError downstream.
    """
    for value, kind in pairs:
        rejection = _reject_bad_id(value, kind)
        if rejection is not None:
            return rejection
    return None


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
    host: str | None = None,
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
    * When supplied, ``host`` must not target an internal / cloud-metadata
      endpoint (loopback, link-local, and—unless
      :data:`~provide.uterm.ai.constants.ALLOW_PRIVATE_HOSTS`—RFC1918 /
      unique-local), blocking SSRF / internal-pivot via model input.
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
        from urllib.parse import urlparse

        parsed_host = urlparse(url).hostname
        if parsed_host is not None and _is_internal_host(parsed_host):
            return {
                "success": False,
                "error": "invalid_host",
                "host": parsed_host,
            }
    if host is not None and _is_internal_host(host):
        return {
            "success": False,
            "error": "invalid_host",
            "host": host,
        }
    return None
