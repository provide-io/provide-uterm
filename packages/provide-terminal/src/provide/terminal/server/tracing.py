#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""OpenTelemetry tracing for API operations via provide.telemetry."""

from __future__ import annotations

from typing import Any

from provide.telemetry import get_tracer

_tracer = get_tracer("provide.terminal.server")


def span(name: str, **attributes: Any) -> _SpanContext:
    """Create a tracing span context manager with attributes."""
    return _SpanContext(_tracer.start_as_current_span(name), attributes)


class _SpanContext:
    """Wrapper to set attributes on span entry."""

    def __init__(self, cm: Any, attributes: dict[str, Any]) -> None:
        self._cm = cm
        self._attributes = attributes
        self._span: Any = None

    def __enter__(self) -> Any:
        self._span = self._cm.__enter__()
        set_attr = getattr(self._span, "set_attribute", None)
        if callable(set_attr):
            for k, v in self._attributes.items():
                if v is not None:
                    set_attr(k, str(v))
        return self._span

    def __exit__(self, *args: Any) -> Any:
        return self._cm.__exit__(*args)

    async def __aenter__(self) -> Any:
        return self.__enter__()

    async def __aexit__(self, *args: Any) -> Any:
        return self.__exit__(*args)
