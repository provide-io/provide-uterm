#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed builder functions for ControlChannel protocol messages.

Each builder returns a fresh dict ready to pass to ``encode_control_frame()``.
Required fields are validated; optional fields are omitted when not provided
so the serialised JSON stays lean.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from provide.uterm.bridge import schemas

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


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
        A dict ready for ``encode_control_frame()``.

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
        A dict ready for ``encode_control_frame()``.

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
        A dict ready for ``encode_control_frame()``.

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
        A dict ready for ``encode_control_frame()``.
    """
    return schemas.ResumeOkFrame(type="resume_ok").model_dump()


def make_resume_failed(reason: str | None = None) -> dict[str, Any]:
    """Build a ``resume_failed`` control message.

    Args:
        reason: Optional human-readable failure reason. Omitted when ``None``.

    Returns:
        A dict ready for ``encode_control_frame()``.
    """
    msg: dict[str, Any] = {"type": "resume_failed"}
    if reason is not None:
        msg["reason"] = reason
    return schemas.ResumeFailedFrame.model_validate(msg).model_dump(exclude_none=True)


def make_link_patterns(patterns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a ``link_patterns`` control message.

    Each entry is validated against
    :class:`~provide.uterm.bridge.schemas.LinkPatternEntry`, the single source of
    truth for the link-pattern field set. Required keys are ``pattern`` (str) and
    ``action`` (one of ``cmd``/``url``/``key``/``focus``); optional keys include
    ``id``, ``flags``, ``group``, ``payload``, ``hover``, ``line_contains`` and
    ``class``. The model is ``extra="forbid"``, so an unmodelled field raises
    instead of being silently dropped on the wire.

    Args:
        patterns: Sequence of pattern mapping dicts.

    Returns:
        A dict ready for ``encode_control_frame()``.

    Raises:
        ValueError: If any pattern entry is malformed (missing or invalid field,
            or an unknown field not modelled by ``LinkPatternEntry``).
    """
    entries: list[schemas.LinkPatternEntry] = []
    for index, entry in enumerate(patterns):
        try:
            entries.append(schemas.LinkPatternEntry.model_validate(dict(entry)))
        except ValidationError as exc:
            raise ValueError(f"make_link_patterns: entry[{index}] is invalid: {exc}") from exc
    frame = schemas.LinkPatternsFrame(type="link_patterns", patterns=entries)
    return frame.model_dump(exclude_none=True, by_alias=True)


def make_presence_update(user_id: str, **fields: Any) -> dict[str, Any]:
    """Build a ``presence_update`` control message.

    Args:
        user_id: The user identifier for this presence update.
        **fields: Arbitrary additional presence fields (e.g. ``scroll_line=5``).

    Returns:
        A dict ready for ``encode_control_frame()``.
    """
    msg: dict[str, Any] = {"type": "presence_update", "user_id": user_id}
    msg.update(fields)
    return schemas.PresenceUpdateFrame.model_validate(msg).model_dump(exclude_none=True)
