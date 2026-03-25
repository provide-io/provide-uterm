# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Tracing context helpers."""

from __future__ import annotations

import contextvars

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)


def set_trace_context(trace_id: str | None, span_id: str | None) -> None:
    _trace_id.set(trace_id)
    _span_id.set(span_id)


def get_trace_context() -> dict[str, str | None]:
    return {"trace_id": _trace_id.get(), "span_id": _span_id.get()}


def get_trace_id() -> str | None:
    """Return the current trace ID without creating a dict."""
    return _trace_id.get()


def get_span_id() -> str | None:
    """Return the current span ID without creating a dict."""
    return _span_id.get()
