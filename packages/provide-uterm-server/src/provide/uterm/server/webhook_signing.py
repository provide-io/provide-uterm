#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared signing helpers for governance webhook payloads."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import cast

_DEFAULT_MAX_AGE_S = 300.0


def build_webhook_signature(secret: str, body: bytes, timestamp: str) -> str:
    """Return ``sha256=<hex>`` over ``"{timestamp}.".encode() + body`` (replay-resistant)."""
    signed = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(
    secret: str | None,
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    *,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    now: float | None = None,
) -> bool:
    """Verify ``X-Uterm-Signature`` over ``ts.body`` and that the timestamp is fresh."""
    # L8a: fail closed when there is no signing key. A signature cannot be
    # authenticated without a shared secret — verifying with an empty key would
    # HMAC over an empty key, which any attacker who knows the body+timestamp
    # can forge. Reject before touching the signature so an empty-key HMAC can
    # never validate, regardless of how this function is reached (a directly
    # constructed WebhookIdentityProvider bypasses the config validator).
    if not (secret or "").strip():
        return False
    if not signature_header or not timestamp_header:
        return False
    try:
        ts_val = float(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts_val) > max_age_s:
        return False
    supplied = signature_header.strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1].strip()
    if not supplied:
        return False
    # The fail-closed guard above guarantees secret is a non-empty str here.
    expected = build_webhook_signature(cast("str", secret), body, timestamp_header).split("=", 1)[1]
    return hmac.compare_digest(supplied, expected)
