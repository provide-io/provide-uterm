#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux integration for the :mod:`provide.uterm.auth` identity frame.

The SSH gateway (:mod:`provide.uterm.gateway`) emits an ``identity``
control frame as the first WebSocket message on connections where a
:class:`~provide.uterm.auth.SSHKeyResolver` accepted the client's
pubkey. This module lets a DeckMux hub consume that frame:

- :func:`parse_identity_frame` turns a raw control-channel dict into a
  :class:`~provide.uterm.auth.ResolvedIdentity`, returning ``None``
  when the frame is malformed or of the wrong type.

- :func:`presence_from_identity` builds a fully-populated
  :class:`UserPresence` from the identity, using claims for
  name/color/role when present and falling back to deterministic
  :func:`generate_name` / :func:`generate_color` derivations when not.

- :func:`identity_as_principal` adapts a ``ResolvedIdentity`` to the
  duck-typed *principal* shape the existing
  :class:`~provide.uterm.deckmux._hub_mixin.DeckMuxMixin` already
  consumes (``subject_id`` + ``display_name``). This means a hub can
  treat an SSH-authenticated user the same way it treats any other
  authenticated principal — no special-case branches.

Trust boundary
--------------

This module performs no trust check of its own. The caller decides
whether to honour an identity frame at all. A DeckMux hub deployed
behind a trusted SSH proxy (e.g. same host, same operator) should
accept it; a hub receiving frames from an untrusted proxy should
either ignore the frame or verify the ``fingerprint`` field against
an independent registry before acting on the ``subject``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from provide.uterm.auth import ResolvedIdentity
from provide.uterm.deckmux._names import generate_color, generate_initials, generate_name
from provide.uterm.deckmux._presence import UserPresence

__all__ = [
    "identity_as_principal",
    "parse_identity_frame",
    "presence_from_identity",
]


_SUPPORTED_VERSIONS = frozenset({1})


def parse_identity_frame(
    frame: Mapping[str, Any], expected_secret: str | bytes | None = None
) -> ResolvedIdentity | None:
    """Extract a :class:`ResolvedIdentity` from a control-channel frame.

    Returns ``None`` when:

    - the frame is not an ``identity`` message,
    - the protocol version isn't understood (forward-compat: unknown
      versions are ignored rather than raising — new frame shapes from
      a newer proxy don't break older hubs),
    - required fields are missing or the wrong type.

    Claims and fingerprint are optional. A malformed ``claims`` value
    (non-mapping) is downgraded to an empty dict rather than rejected
    — we'd rather surface an identity with no extra metadata than lose
    the subject entirely.
    """
    if frame.get("type") != "identity":
        return None
    version = frame.get("version")
    if version not in _SUPPORTED_VERSIONS:
        return None
    subject = frame.get("subject")
    if not isinstance(subject, str) or not subject:
        return None
    raw_claims = frame.get("claims")
    claims: dict[str, Any] = dict(raw_claims) if isinstance(raw_claims, Mapping) else {}
    fingerprint_raw = frame.get("fingerprint", "")
    fingerprint = fingerprint_raw if isinstance(fingerprint_raw, str) else ""
    if expected_secret:
        signature = frame.get("signature")
        if not signature or not isinstance(signature, str):
            return None

        claims_str = json.dumps(claims, sort_keys=True, separators=(",", ":"))
        transport = frame.get("transport", "")
        if not isinstance(transport, str):
            transport = ""

        canonical = f"{version}:{subject}:{fingerprint}:{transport}:{claims_str}"
        secret_bytes = expected_secret if isinstance(expected_secret, bytes) else expected_secret.encode("utf-8")
        expected_sig = hmac.new(secret_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            return None

    return ResolvedIdentity(subject=subject, claims=claims, fingerprint=fingerprint)


def presence_from_identity(
    identity: ResolvedIdentity,
    connection_id: str,
    *,
    taken_colors: frozenset[str] = frozenset(),
    role: str = "",
) -> UserPresence:
    """Build a :class:`UserPresence` from a resolved identity.

    Resolution order:

    - ``name``  = ``claims.display_name`` / ``claims.display`` /
      derived from the subject (``sre:alice`` → ``alice``) /
      ``generate_name(connection_id)``
    - ``color`` = ``claims.color`` / ``generate_color(connection_id, taken)``
    - ``role``  = ``claims.role`` / caller-supplied ``role``
    - ``initials`` = :func:`generate_initials` from the final ``name``

    The ``connection_id`` is only used for deterministic fallback
    generation; the ``user_id`` on the resulting presence is always
    ``identity.subject`` so repeated connections from the same user
    collapse to one presence entry.
    """
    claims = identity.claims or {}
    name = _first_nonempty(
        _str_or_none(claims.get("display_name")),
        _str_or_none(claims.get("display")),
        _name_from_subject(identity.subject),
    ) or generate_name(connection_id)
    color = _str_or_none(claims.get("color")) or generate_color(connection_id, taken=taken_colors)
    resolved_role = _str_or_none(claims.get("role")) or role
    return UserPresence(
        user_id=identity.subject,
        name=name,
        color=color,
        role=resolved_role,
        initials=generate_initials(name),
    )


@dataclass(frozen=True)
class _IdentityPrincipal:
    """Duck-typed principal view over a :class:`ResolvedIdentity`.

    Exposes ``subject_id`` and ``display_name`` — the exact attribute
    names the existing :class:`DeckMuxMixin` already introspects via
    ``getattr(principal, …)``. That keeps the hub code path single-track:
    callers pass an ``_IdentityPrincipal`` wherever they'd pass a
    traditional HTTP principal, and the mixin doesn't need to know the
    difference.
    """

    subject_id: str
    display_name: str
    identity: ResolvedIdentity


def identity_as_principal(identity: ResolvedIdentity) -> _IdentityPrincipal:
    """Adapt a :class:`ResolvedIdentity` to the DeckMux principal shape."""
    claims = identity.claims or {}
    display = (
        _first_nonempty(
            _str_or_none(claims.get("display_name")),
            _str_or_none(claims.get("display")),
            _name_from_subject(identity.subject),
        )
        or identity.subject
    )
    return _IdentityPrincipal(
        subject_id=identity.subject,
        display_name=display,
        identity=identity,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _first_nonempty(*values: str | None) -> str | None:
    for v in values:
        if v:
            return v
    return None


def _str_or_none(value: Any) -> str | None:
    """Coerce *value* to a non-empty string, else ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _name_from_subject(subject: str) -> str | None:
    """Extract a display-friendly name from a subject like ``'sre:alice'``.

    Returns the text after the first colon. ``'alice'`` → ``'alice'``
    (no colon). Empty after-colon portions return ``None`` so the
    caller can fall through to the next candidate.
    """
    if ":" not in subject:
        return subject.strip() or None
    _, _, tail = subject.partition(":")
    return tail.strip() or None
