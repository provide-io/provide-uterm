#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The capture-owner hook client and its validation.

Decision 1 of the capture-session-ownership ARD: when ``capture_owner`` is
``hook``, uterm asks a configured URL who owns a login and uses the answer.
This module is the client that makes that call and validates the answer.

The hook is called after the session is created, never before: the session is
created ownerless exactly as today, its capture socket is bound, and the hook
runs as a task beside it. The login itself never waits on anything uterm does.

**Fingerprints.** The server fingerprints each public-key blob as OpenSSH
does -- ``SHA256:`` plus the unpadded base64 of the SHA-256 of the decoded
blob -- and checks that the blob's own embedded key-type string equals the
line's; a mismatch drops that key. The raw key never leaves the server; the
hook is sent fingerprints.

**The call.** A POST with a signed body (HMAC-SHA256 over
``"<timestamp>." + body``), a per-request nonce, and the session id. The
answer must be a 200 whose body is signed the same way and echoes the nonce.

**Validation.** The owner must match ``^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$``,
must not start with ``share:`` (the prefix uterm gives share viewers), and
``visibility`` must be ``private`` or ``operator`` (never ``public``). The
merged definition must then pass ``SessionDefinition`` validation.

**Failure** is anything but a verified, valid answer within the timeout: the
session stays ownerless and uterm logs ``pam_capture_owner_hook_failed`` at
warning. It is not retried: the existing admin routes are the fallback.

Signing reuses ``webhook_signing.build_webhook_signature`` /
``verify_webhook_signature`` and the nonce and replay-cache handling
``auth_webhook.py`` applies to the identity-provider webhook.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from provide.uterm.server import _http
from provide.uterm.server.egress import EgressBlockedError, assert_webhook_target_allowed
from provide.uterm.server.webhook_signing import build_webhook_signature, verify_webhook_signature

if TYPE_CHECKING:
    from collections.abc import Mapping

    from provide.uterm.server.config_schema import PamConfig

logger = logging.getLogger(__name__)

# The owner pattern: a machine answer arriving without an admin behind it, so
# stricter than the live PATCH's (which takes any string). Must not start with
# "share:" (the prefix uterm gives share viewers; an owner of that shape would
# hand owner rights to a share token's subject).
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SHARE_PREFIX = "share:"

# A public-key line from SSH_AUTH_INFO_0: "publickey <key type> <base64 blob>".
# At most four, each checked against this and dropped whole if it fails. A line
# is never truncated: half a key fingerprints as a different key.
_PUBLICKEY_LINE = re.compile(r"^publickey ([A-Za-z0-9@.-]{1,64}) ([A-Za-z0-9+/]{1,4096}={0,2})$")

# The freshness window for a hook response's timestamp. Same as the IdP webhook.
_HOOK_MAX_AGE_S = 300.0


@dataclass(frozen=True)
class SshKey:
    """A public key's type and its OpenSSH fingerprint, as sent to the hook."""

    key_type: str
    fingerprint: str


@dataclass(frozen=True)
class HookAnswer:
    """The owner and visibility a hook returned for a capture session."""

    owner: str | None
    visibility: str | None


class HookError(Exception):
    """A hook call failed. The session stays ownerless; the reason is logged."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def fingerprint_ssh_keys(ssh_auth_info: list[str] | None) -> list[SshKey]:
    """Fingerprint each public-key line as OpenSSH does.

    Each line is ``publickey <key type> <base64 blob>``. The blob is decoded,
    its embedded key-type string is checked against the line's, and the SHA-256
    of the decoded blob is base64-encoded (unpadded) and prefixed with
    ``SHA256:``. A line that fails any check is dropped whole. At most four are
    returned; the field is omitted when nothing passes.
    """

    if not ssh_auth_info:
        return []
    keys: list[SshKey] = []
    for line in ssh_auth_info[:4]:
        match = _PUBLICKEY_LINE.match(line)
        if match is None:
            continue
        key_type = match.group(1)
        blob_b64 = match.group(2)
        try:
            blob = base64.b64decode(blob_b64, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        # The blob starts with a 4-byte length prefix, then the key-type string.
        # Check that the embedded type matches the line's; a mismatch drops the key.
        embedded_type = _extract_key_type(blob)
        if embedded_type is None or embedded_type != key_type:
            continue
        digest = hashlib.sha256(blob).digest()
        fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        keys.append(SshKey(key_type=key_type, fingerprint=fingerprint))
    return keys


def _extract_key_type(blob: bytes) -> str | None:
    """Read the key-type string from the start of an OpenSSH public-key blob.

    The blob's first field is a 4-byte big-endian length, then that many bytes
    of the key-type string (e.g. ``ssh-ed25519``). Returns ``None`` on a
    truncated or malformed blob.
    """

    if len(blob) < 4:
        return None
    length = struct.unpack(">I", blob[:4])[0]
    if length > len(blob) - 4 or length > 256:
        return None
    try:
        return blob[4 : 4 + length].decode("ascii")
    except UnicodeDecodeError:
        return None


def validate_owner(owner: str | None) -> str | None:
    """Validate and return the owner, or raise ``HookError``.

    The owner must match ``^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$`` and must not
    start with ``share:``. ``None`` is a deliberate "this login has no owner"
    answer: logged at info, not a warning, and returned as-is.
    """

    if owner is None:
        return None
    if owner.startswith(_SHARE_PREFIX):
        raise HookError("owner_share_prefix")
    if not _OWNER_PATTERN.match(owner):
        raise HookError("owner_pattern")
    return owner


def validate_visibility(visibility: str | None) -> str | None:
    """Validate and return the visibility, or raise ``HookError``.

    ``"private"`` or ``"operator"``; absent keeps ``"operator"``. ``"public"``
    is refused: a hook answers who owns a login, not who may watch it.
    """

    if visibility is None:
        return None
    if visibility not in ("private", "operator"):
        raise HookError("visibility_public" if visibility == "public" else "visibility_invalid")
    return visibility


def validate_hook_answer(body: bytes, nonce: str) -> HookAnswer:
    """Parse and validate a hook response body.

    The body must be valid JSON with a ``nonce`` that echoes the request's, an
    ``owner`` (string or null), and an optional ``visibility``. The owner and
    visibility are validated; any failure raises ``HookFailure``.
    """

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HookError("body_unparseable") from exc
    if not isinstance(data, dict):
        raise HookError("body_not_object")
    echoed_nonce = data.get("nonce")
    if not isinstance(echoed_nonce, str) or echoed_nonce != nonce:
        raise HookError("nonce_mismatch")
    owner = data.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise HookError("owner_not_string")
    visibility = data.get("visibility")
    if visibility is not None and not isinstance(visibility, str):
        raise HookError("visibility_not_string")
    return HookAnswer(
        owner=validate_owner(owner),
        visibility=validate_visibility(visibility),
    )


async def call_capture_owner_hook(
    config: PamConfig,
    *,
    session_id: str,
    username: str,
    tty: str | None,
    pid: int,
    start_ticks: int | None,
    ssh_keys: list[SshKey],
    adopted: bool = False,
    http_post=None,
    clock=time.time,
) -> HookAnswer:
    """Ask the configured hook who owns a capture session's login.

    Makes a signed POST to ``capture_owner_hook_url``, waits for a signed 200
    response that echoes the nonce, and validates the owner and visibility.
    Any failure raises :class:`HookFailure`; the caller logs it and leaves the
    session ownerless.

    ``http_post`` is injectable for testing (no real HTTP in the mutation
    suite). It must be an async callable taking ``(url, headers, body,
    timeout_s)`` and returning ``(status_code, response_headers, response_body)``.
    """

    url = config.capture_owner_hook_url
    secret = config.capture_owner_hook_secret
    timeout_s = config.capture_owner_hook_timeout_s
    if url is None or secret is None:
        raise HookError("not_configured")

    # The egress chokepoint: cloud-metadata IPs are never a legitimate target.
    try:
        await assert_webhook_target_allowed(url)
    except EgressBlockedError as exc:
        raise HookError("egress_blocked") from exc

    nonce = secrets.token_urlsafe(16)
    timestamp = str(int(clock()))
    body = json.dumps(
        {
            "version": 1,
            "action": "capture_owner",
            "nonce": nonce,
            "session_id": session_id,
            "login": {
                "username": username,
                "tty": tty,
                "pid": pid,
                "start_ticks": start_ticks,
            },
            "ssh_keys": [{"key_type": k.key_type, "fingerprint": k.fingerprint} for k in ssh_keys],
            "adopted": adopted,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = build_webhook_signature(secret, body, timestamp)
    headers = {
        "Content-Type": "application/json",
        "X-Uterm-Timestamp": timestamp,
        "X-Uterm-Signature": signature,
        "X-Uterm-Nonce": nonce,
    }

    if http_post is None:
        http_post = _http_post
    try:
        status_code, response_headers, response_body = await http_post(url, headers, body, timeout_s)
    except TimeoutError as exc:
        raise HookError("timeout") from exc
    except OSError as exc:
        raise HookError("connection_refused") from exc

    if status_code != 200:
        raise HookError(f"status_{status_code}")

    # The response must be signed the same way and echo the nonce.
    resp_sig = response_headers.get("X-Uterm-Signature") if response_headers else None
    resp_ts = response_headers.get("X-Uterm-Timestamp") if response_headers else None
    if not verify_webhook_signature(
        secret,
        response_body,
        resp_sig,
        resp_ts,
        max_age_s=_HOOK_MAX_AGE_S,
        now=clock(),
    ):
        raise HookError("signature_invalid")

    return validate_hook_answer(response_body, nonce)


async def _http_post(
    url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
) -> tuple[int, Mapping[str, str], bytes]:
    """The real HTTP client. Uses the package's shared httpx2 client.

    Returns ``(status_code, response_headers, response_body)``. httpx2's
    exception tree is not rooted at stdlib ``OSError``/``TimeoutError``; this
    function re-raises the two kinds the hook cares about back to stdlib so
    :func:`call_capture_owner_hook`'s except clauses stay free of httpx imports.
    """

    import httpx2

    try:
        async with _http.async_client(timeout=timeout_s) as http:
            resp = await http.post(url, content=body, headers=dict(headers))
    except httpx2.TimeoutException as exc:
        raise TimeoutError(str(exc)) from exc
    except _http.HTTPError as exc:
        raise OSError(str(exc)) from exc
    return resp.status_code, dict(resp.headers), resp.content
