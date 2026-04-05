# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Shared header extraction helpers."""

from __future__ import annotations

__all__ = ["get_header"]

from typing import Any


def get_header(scope: dict[str, Any], key: bytes) -> str | None:
    """Return a decoded header value or None for malformed/unsupported values."""
    for name, value in scope.get("headers", []):
        if not isinstance(name, (bytes, str)):
            continue
        if _normalize_header_name(name) != key:
            continue
        return _decode_header_value(value)
    return None


def _normalize_header_name(name: bytes | str) -> bytes:
    if isinstance(name, bytes):
        return name.lower()
    lowered = name.lower()
    if not lowered.isascii():
        return b""
    return lowered.encode()


def _decode_header_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")  # pragma: no mutate
        except UnicodeDecodeError:
            return value.decode("latin-1")  # pragma: no mutate
    return None
