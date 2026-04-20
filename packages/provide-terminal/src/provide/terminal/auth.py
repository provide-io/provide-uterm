#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pluggable SSH-key-based authentication for provide-terminal gateways.

This module defines the boundary between *transport* (which provide-terminal
owns — the SSH handshake, the pubkey fingerprint) and *identity* (which the
consuming application owns — who does this key belong to?).

Consumers plug in an :class:`SSHKeyResolver` implementation that maps a
fingerprint to a :class:`ResolvedIdentity`. The gateway calls the resolver
during SSH public-key auth; on a hit, the identity is threaded through the
downstream WebSocket via a control-channel ``identity`` hello frame so the
upstream server can auto-log-in the user without a password round trip.

Two reference resolvers ship with provide-terminal:

- :class:`NullResolver` — always returns ``None``; preserves the historical
  "accept any key, no identity" behaviour. Used by default.
- :class:`AuthorizedKeysFileResolver` — parses an OpenSSH ``authorized_keys``
  file and resolves by fingerprint match. The `options` field on each line
  is surfaced as ``claims``, so consumers like DeckMux can tag keys with
  ``role="oncall"`` / ``display_name="alice"`` etc.

Trust model
-----------

The gateway runs in a proxy process that the player connects to. When a
proxy asserts an identity to the upstream server (via the hello frame),
the server is trusting the proxy to have done the auth correctly. This is
a pure bearer-trust model — the upstream should only opt in when the
proxy is under the same operational control (e.g. DeckMux's proxy and
multiplexer co-deployed on one host). For hostile-proxy scenarios (uwarp's
player-laptop proxy talking to Cloudflare), the upstream should ignore
identity frames and re-authenticate independently.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AuthorizedKeysFileResolver",
    "NullResolver",
    "ResolvedIdentity",
    "SSHKeyResolver",
    "fingerprint_from_openssh_blob",
]


@dataclass(frozen=True)
class ResolvedIdentity:
    """An identity successfully resolved from an SSH public key.

    Attributes:
        subject: Opaque, consumer-defined identifier — e.g. ``"player:42"``,
            ``"sre:alice"``. The gateway never parses this; it only forwards.
        claims: Free-form key/value map of additional attributes the consumer
            wants to send along (groups, display name, theme, etc.).
        fingerprint: The OpenSSH-style SHA256 fingerprint (``"SHA256:…"``)
            that was resolved. Populated by the gateway; resolvers may leave
            it empty.
    """

    subject: str
    claims: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = ""


@runtime_checkable
class SSHKeyResolver(Protocol):
    """Asynchronous map from SSH public key → application identity.

    Implementations are called once per inbound SSH connection, during the
    ``validate_public_key`` phase of the handshake. Return ``None`` to
    signal "this key is not known to me" — the gateway then either falls
    through to password auth or rejects the connection depending on the
    ``require_resolver`` gateway setting.

    Implementations must be coroutine-safe. A sync implementation can be
    exposed async-style by wrapping its work in :func:`asyncio.to_thread`.
    """

    async def resolve(
        self,
        fingerprint: str,
        *,
        pubkey_blob: bytes,
        username: str,
    ) -> ResolvedIdentity | None:
        """Resolve a public key to an identity, or return None if unknown.

        Args:
            fingerprint: OpenSSH-style SHA256 fingerprint ("SHA256:…").
            pubkey_blob: Full OpenSSH public-key bytes (``ssh-ed25519 AAAAC3…``
                or the binary SSH wire format). Pass-through; implementations
                may ignore this if fingerprint alone is sufficient.
            username: The SSH username the client offered during login.
                May be empty. Useful when the same key should resolve to
                different identities under different usernames.
        """


class NullResolver:
    """Resolver that never resolves anything.

    Equivalent to not configuring a resolver at all — exists so callers can
    pass a non-None resolver unconditionally. ``resolve`` always returns
    ``None``, so the gateway falls through to password auth (or rejects if
    ``require_resolver`` is True — typically a misconfiguration).
    """

    async def resolve(
        self,
        fingerprint: str,  # noqa: ARG002
        *,
        pubkey_blob: bytes,  # noqa: ARG002
        username: str,  # noqa: ARG002
    ) -> ResolvedIdentity | None:
        return None


def fingerprint_from_openssh_blob(blob: bytes) -> str:
    """Compute an OpenSSH-style SHA256 fingerprint from raw key bytes.

    Accepts either:
    - Binary SSH wire format (first 4 bytes = algorithm-name length prefix)
    - Text form starting with ``ssh-`` / ``ecdsa-`` / ``sk-`` / ``ssh-ed25519``

    Returns a string like ``"SHA256:1234…"`` matching ``ssh-keygen -lf`` output.

    Raises:
        ValueError: if the blob cannot be parsed as a public key.
    """
    binary = _coerce_to_binary_pubkey(blob)
    digest = hashlib.sha256(binary).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


def _coerce_to_binary_pubkey(blob: bytes) -> bytes:
    """Extract the base64-decoded SSH wire-format bytes from *blob*.

    Handles the two common input shapes:
    - ``b"ssh-ed25519 AAAAC3…[ comment]"`` (OpenSSH public-key text format)
    - the raw SSH wire format (passed through unchanged)
    """
    stripped = blob.strip()
    if stripped.startswith((b"ssh-", b"ecdsa-", b"sk-ssh-", b"sk-ecdsa-")):
        # Text form: second whitespace-separated token is the base64 payload.
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            raise ValueError("malformed OpenSSH public key line")
        try:
            return base64.b64decode(parts[1], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f"invalid base64 in public key: {exc}") from exc
    # Assume it's already the binary wire format.
    return stripped


class AuthorizedKeysFileResolver:
    """Resolve identities against a file in OpenSSH ``authorized_keys`` format.

    Each line in the file is a standard OpenSSH public-key entry::

        [options] <keytype> <base64-payload> [comment]

    Optional ``options`` are comma-separated ``key="value"`` / ``key`` flags
    (OpenSSH's ``sshd_config(5)`` grammar). Recognised here are:

    - ``subject="…"`` — explicit subject to use; defaults to the comment if
      absent, or ``"key:<fp>"`` if neither is set.
    - ``claim-<name>="…"`` — a single claim entry on the resulting identity.
      Multiple allowed; e.g. ``claim-role="oncall",claim-display="alice"``.

    Unrecognised OpenSSH options (``no-pty``, ``command="…"``, etc.) are
    preserved in the ``claims`` map under a ``_options`` key so the upstream
    consumer can inspect them if desired.

    Example line::

        subject="sre:alice",claim-role="oncall" ssh-ed25519 AAAAC3N… alice@laptop

    The file is read and parsed lazily on each ``resolve`` call — that keeps
    this class simple and makes key-rotation pick up immediately. For
    deployments with a huge ``authorized_keys`` file, wrap this in a caching
    resolver.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def resolve(
        self,
        fingerprint: str,
        *,
        pubkey_blob: bytes,  # noqa: ARG002 — file keyed by fingerprint only
        username: str,  # noqa: ARG002 — one key per line, no per-user gating
    ) -> ResolvedIdentity | None:
        entries = await asyncio.to_thread(self._load_entries)
        for entry in entries:
            if entry.fingerprint == fingerprint:
                return ResolvedIdentity(
                    subject=entry.subject,
                    claims=entry.claims,
                    fingerprint=fingerprint,
                )
        return None

    def _load_entries(self) -> list[_AuthorizedKeyEntry]:
        """Parse the file into a list of entries — sync, caller wraps in a thread."""
        if not self._path.exists():
            return []
        out: list[_AuthorizedKeyEntry] = []
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(_parse_authorized_keys_line(line))
            except ValueError:
                # Skip malformed lines rather than abort the whole file —
                # one bad entry shouldn't lock everybody out.
                continue
        return out


@dataclass(frozen=True)
class _AuthorizedKeyEntry:
    fingerprint: str
    subject: str
    claims: Mapping[str, Any]


def _parse_authorized_keys_line(line: str) -> _AuthorizedKeyEntry:
    """Parse one non-empty/non-comment line of an authorized_keys file.

    The grammar (simplified) is::

        [options_csv] keytype base64_payload [comment...]

    Options are detected by the first token not matching a keytype prefix.
    """
    # Decide whether the line starts with an options field. Options end at
    # the first whitespace that isn't inside a quoted value.
    keytype_prefixes = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")
    first_token_end = _find_first_token_end(line)
    first_token = line[:first_token_end]

    if first_token.startswith(keytype_prefixes):
        options_str = ""
        rest = line
    else:
        options_str = first_token
        rest = line[first_token_end:].lstrip()

    parts = rest.split(None, 2)
    if len(parts) < 2:
        raise ValueError("missing key payload")
    keytype, payload = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""

    blob_text = f"{keytype} {payload}"
    fp = fingerprint_from_openssh_blob(blob_text.encode("ascii"))

    opts = _parse_options(options_str) if options_str else {}

    subject = opts.pop("subject", None) or comment.strip() or f"key:{fp}"
    claims: dict[str, Any] = {}
    leftover_options: dict[str, Any] = {}
    for key, value in opts.items():
        if key.startswith("claim-"):
            claims[key.removeprefix("claim-")] = value
        else:
            leftover_options[key] = value
    if leftover_options:
        claims["_options"] = leftover_options

    return _AuthorizedKeyEntry(fingerprint=fp, subject=subject, claims=claims)


def _find_first_token_end(line: str) -> int:
    """Return the index where the first top-level whitespace appears.

    Respects double-quoted substrings so ``command="echo hi",no-pty`` is a
    single token. Backslash escapes are NOT interpreted — OpenSSH's own
    parser doesn't either inside option values.
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch.isspace() and not in_quotes:
            return i
    return len(line)


def _parse_options(options_str: str) -> dict[str, str | bool]:
    """Parse the comma-separated OpenSSH options field into a dict.

    ``key="value"`` → ``{"key": "value"}``; ``flag`` → ``{"flag": True}``.
    Values are returned as strings; callers can re-interpret if needed.
    """
    out: dict[str, str | bool] = {}
    for token in _split_options(options_str):
        if "=" in token:
            key, _, value = token.partition("=")
            out[key.strip()] = value.strip().strip('"')
        else:
            out[token.strip()] = True
    return out


def _split_options(options_str: str) -> list[str]:
    """Split an options CSV string on commas that aren't inside quotes."""
    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in options_str:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out
