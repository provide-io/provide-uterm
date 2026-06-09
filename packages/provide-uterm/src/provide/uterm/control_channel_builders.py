#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed builder functions for ControlChannel protocol messages.

Each builder returns a fresh dict ready to pass to ``encode_control()``.
Required fields are validated; optional fields are omitted when not provided
so the serialised JSON stays lean.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from provide.uterm.bridge import schemas

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Valid actions for link-pattern entries.
_LINK_PATTERN_ACTIONS = frozenset({"cmd", "url", "key", "focus"})

# Optional keys allowed in a link-pattern entry (beyond required "pattern"/"action").
_LINK_PATTERN_OPTIONAL_KEYS = frozenset({"id", "flags", "group", "payload", "hover", "class"})


def _canonical_identity_signature_payload(
    *,
    version: object,
    subject: str,
    fingerprint: str,
    transport: str,
    claims: Mapping[str, Any],
) -> bytes:
    claims_json = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    return f"{version}:{subject}:{fingerprint}:{transport}:{claims_json}".encode()


def make_identity(
    subject: str,
    claims: Mapping[str, Any] | None = None,
    fingerprint: str | None = None,
    transport: str | None = None,
    secret: str | bytes | None = None,
) -> dict[str, Any]:
    """Build an ``identity`` control message.

    Args:
        subject: Non-empty identity subject string (e.g. ``"user:alice"``).
        claims: Optional mapping of additional identity claims. Omitted when ``None``.
        fingerprint: SSH key fingerprint or empty string.
        transport: Transport type string (default ``"ssh"``).

    Returns:
        A dict ready for ``encode_control()``.

    Raises:
        ValueError: If *subject* is empty.
    """
    if not subject:
        raise ValueError("make_identity: 'subject' must be a non-empty string")
    fingerprint_value = "" if fingerprint is None else fingerprint
    transport_value = "ssh" if transport is None else transport
    msg: dict[str, Any] = {
        "type": "identity",
        "version": 1,
        "subject": subject,
        "fingerprint": fingerprint_value,
        "transport": transport_value,
    }
    if claims is not None:
        msg["claims"] = dict(claims)

    if secret:
        secret_bytes = secret if isinstance(secret, bytes) else secret.encode()
        payload = _canonical_identity_signature_payload(
            version=msg["version"],
            subject=msg["subject"],
            fingerprint=fingerprint_value,
            transport=transport_value,
            claims=msg.get("claims", {}),
        )
        signature = hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()
        msg["signature"] = signature

    return schemas.IdentityFrame.model_validate(msg).model_dump(exclude_none=True)


def make_session_token(token: str, player_id: int | None = None) -> dict[str, Any]:
    """Build a ``session_token`` control message.

    Args:
        token: Non-empty session token string.
        player_id: Optional player identifier. Omitted when ``None``.

    Returns:
        A dict ready for ``encode_control()``.

    Raises:
        ValueError: If *token* is empty.
    """
    if not token:
        raise ValueError("make_session_token: 'token' must be a non-empty string")
    msg: dict[str, Any] = {"type": "session_token", "token": token}
    if player_id is not None:
        msg["player_id"] = player_id
    return schemas.SessionTokenFrame.model_validate(msg).model_dump(exclude_none=True)


def make_resume(token: str, player_id: int | None = None) -> dict[str, Any]:
    """Build a ``resume`` control message.

    Args:
        token: Non-empty resume token string.
        player_id: Optional player identifier. Omitted when ``None``.

    Returns:
        A dict ready for ``encode_control()``.

    Raises:
        ValueError: If *token* is empty.
    """
    if not token:
        raise ValueError("make_resume: 'token' must be a non-empty string")
    msg: dict[str, Any] = {"type": "resume", "token": token}
    if player_id is not None:
        msg["player_id"] = player_id
    return schemas.ResumeFrame.model_validate(msg).model_dump(exclude_none=True)


def make_resume_ok() -> dict[str, Any]:
    """Build a ``resume_ok`` control message.

    Returns:
        A dict ready for ``encode_control()``.
    """
    return schemas.ResumeOkFrame(type="resume_ok").model_dump()


def make_resume_failed(reason: str | None = None) -> dict[str, Any]:
    """Build a ``resume_failed`` control message.

    Args:
        reason: Optional human-readable failure reason. Omitted when ``None``.

    Returns:
        A dict ready for ``encode_control()``.
    """
    msg: dict[str, Any] = {"type": "resume_failed"}
    if reason is not None:
        msg["reason"] = reason
    return schemas.ResumeFailedFrame.model_validate(msg).model_dump(exclude_none=True)


def _validate_link_pattern_entry(entry: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Validate and normalise a single link-pattern entry.

    Required keys: ``pattern`` (str), ``action`` (one of cmd/url/key/focus).
    Optional keys: ``id``, ``flags``, ``group``, ``payload``, ``hover``, ``class``.

    Args:
        entry: The raw pattern mapping to validate.
        index: Zero-based position in the patterns list (for error messages).

    Returns:
        A clean dict with required fields and any present optional fields.

    Raises:
        ValueError: If required fields are missing or ``action`` is invalid.
    """
    if "pattern" not in entry:
        raise ValueError(f"make_link_patterns: entry[{index}] missing required field 'pattern'")
    if "action" not in entry:
        raise ValueError(f"make_link_patterns: entry[{index}] missing required field 'action'")
    action = entry["action"]
    if action not in _LINK_PATTERN_ACTIONS:
        valid = ", ".join(sorted(_LINK_PATTERN_ACTIONS))
        raise ValueError(f"make_link_patterns: entry[{index}] has invalid action {action!r}; must be one of: {valid}")
    result: dict[str, Any] = {"pattern": entry["pattern"], "action": action}
    for key in _LINK_PATTERN_OPTIONAL_KEYS:
        if key in entry:
            result[key] = entry[key]
    return result


def make_link_patterns(patterns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a ``link_patterns`` control message.

    Each entry in *patterns* must contain:
    - ``pattern`` (str): the regex or literal pattern to match.
    - ``action`` (str): one of ``"cmd"``, ``"url"``, ``"key"``, ``"focus"``.

    Optional per-entry keys: ``id``, ``flags``, ``group``, ``payload``, ``hover``, ``class``.

    Args:
        patterns: Sequence of pattern mapping dicts.

    Returns:
        A dict ready for ``encode_control()``.

    Raises:
        ValueError: If any pattern entry is malformed.
    """
    validated = [_validate_link_pattern_entry(entry, i) for i, entry in enumerate(patterns)]
    msg = {"type": "link_patterns", "patterns": validated}
    return schemas.LinkPatternsFrame.model_validate(msg).model_dump(exclude_none=True, by_alias=True)


def make_presence_update(user_id: str, **fields: Any) -> dict[str, Any]:
    """Build a ``presence_update`` control message.

    Args:
        user_id: The user identifier for this presence update.
        **fields: Arbitrary additional presence fields (e.g. ``scroll_line=5``).

    Returns:
        A dict ready for ``encode_control()``.
    """
    msg: dict[str, Any] = {"type": "presence_update", "user_id": user_id}
    msg.update(fields)
    return schemas.PresenceUpdateFrame.model_validate(msg).model_dump(exclude_none=True)
