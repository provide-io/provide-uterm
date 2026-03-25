# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Tracing decorators."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from provide.telemetry.backpressure import release, try_acquire
from provide.telemetry.sampling import should_sample
from provide.telemetry.tracing.context import get_span_id, get_trace_id, set_trace_context
from provide.telemetry.tracing.provider import _sync_otel_trace_context, get_tracer

P = ParamSpec("P")
R = TypeVar("R")


def trace(name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = str(name or getattr(fn, "__name__", fn.__class__.__name__))

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                if not should_sample("traces", span_name):
                    return await fn(*args, **kwargs)
                ticket = try_acquire("traces")
                if ticket is None:
                    return await fn(*args, **kwargs)
                prev_trace = get_trace_id()
                prev_span = get_span_id()
                with get_tracer(fn.__module__).start_as_current_span(span_name):
                    _sync_otel_trace_context()
                    try:
                        return await fn(*args, **kwargs)
                    finally:
                        set_trace_context(prev_trace, prev_span)
                        release(ticket)

            return cast(Callable[P, R], async_wrapper)  # pragma: no mutate

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not should_sample("traces", span_name):
                return fn(*args, **kwargs)
            ticket = try_acquire("traces")
            if ticket is None:
                return fn(*args, **kwargs)
            prev_trace = get_trace_id()
            prev_span = get_span_id()
            with get_tracer(fn.__module__).start_as_current_span(span_name):
                _sync_otel_trace_context()
                try:
                    return fn(*args, **kwargs)
                finally:
                    set_trace_context(prev_trace, prev_span)
                    release(ticket)

        return cast(Callable[P, R], sync_wrapper)  # pragma: no mutate

    return decorator
