#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared signing helpers for governance webhook payloads."""

from __future__ import annotations

import hashlib
import hmac


def build_webhook_signature(secret: str, body: bytes) -> str:
    """Return the canonical ``sha256=<hex>`` signature for *body*."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify an incoming ``X-Uterm-Signature`` header.

    Accepts either ``sha256=<hex>`` (preferred) or a bare hex digest for
    compatibility with simple receivers.
    """
    if not signature_header:
        return False
    supplied = signature_header.strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1].strip()
    if not supplied:
        return False
    expected = build_webhook_signature(secret, body).split("=", 1)[1]
    return hmac.compare_digest(supplied, expected)
