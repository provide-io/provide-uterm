# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Event schema validation."""

from __future__ import annotations

__all__ = [
    "EventSchemaError",
    "event_name",
    "validate_event_name",
    "validate_required_keys",
]

import re

from provide.telemetry.exceptions import TelemetryError

_SEG = r"[a-z][a-z0-9_]*"
_EVENT_RE = re.compile(rf"^{_SEG}(?:\.{_SEG}){{2,4}}$")
_SEGMENT_RE = re.compile(rf"^{_SEG}$")
_MIN_SEGMENTS = 3
_MAX_SEGMENTS = 5


class EventSchemaError(TelemetryError, ValueError):
    """Raised when an event violates schema policy."""


def event_name(*segments: str) -> str:
    """Build an event name from dot-separated segments.

    In strict mode (``strict_schema`` or ``strict_event_name``): enforces 3-5
    lowercase/underscore segments.  In relaxed mode (default): accepts 1+
    segments with no format validation.
    """
    from provide.telemetry.runtime import _is_strict_event_name

    strict = _is_strict_event_name()
    if strict:
        if not (_MIN_SEGMENTS <= len(segments) <= _MAX_SEGMENTS):
            raise EventSchemaError(f"expected {_MIN_SEGMENTS}-{_MAX_SEGMENTS} segments, got {len(segments)}")
        for i, value in enumerate(segments):
            if not _SEGMENT_RE.match(value):
                raise EventSchemaError(f"invalid event segment: segment[{i}]={value}")
    elif len(segments) == 0:
        raise EventSchemaError("event_name requires at least 1 segment")
    return ".".join(segments)


def validate_event_name(name: str, strict_event_name: bool) -> None:
    if not strict_event_name:
        return
    if not _EVENT_RE.match(name):
        raise EventSchemaError(f"invalid event name: {name}")


def validate_required_keys(data: dict[str, object], required_keys: tuple[str, ...]) -> None:
    missing = [key for key in required_keys if key not in data]
    if missing:
        raise EventSchemaError(f"missing required keys: {', '.join(sorted(missing))}")
