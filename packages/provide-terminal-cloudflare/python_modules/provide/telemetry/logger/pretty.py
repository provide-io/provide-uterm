# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Pretty ANSI log renderer for CLI / terminal output."""

from __future__ import annotations

from typing import Any

LEVEL_COLORS: dict[str, str] = {
    "critical": "\033[31;1m",  # bold red
    "error": "\033[31m",  # red
    "warning": "\033[33m",  # yellow
    "info": "\033[32m",  # green
    "debug": "\033[34m",  # blue
    "trace": "\033[36m",  # cyan
}
RESET = "\033[0m"
DIM = "\033[2m"
_LEVEL_PAD = 9  # "critical" = 8 chars; pad to 9 for consistent alignment

NAMED_COLORS: dict[str, str] = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "none": "",
}


def resolve_color(name: str) -> str:
    """Return ANSI escape for a named color, or '' for empty/unknown names."""
    return NAMED_COLORS.get(name, "")


class PrettyRenderer:
    """Structlog-compatible renderer that emits ANSI-coloured log lines."""

    def __init__(  # pragma: no mutate
        self,
        colors: bool = True,  # pragma: no mutate
        key_color: str = "",  # pragma: no mutate
        value_color: str = "",  # pragma: no mutate
        fields: tuple[str, ...] = (),  # pragma: no mutate
    ) -> None:
        self._colors = colors  # pragma: no mutate
        self._key_color = key_color  # pragma: no mutate
        self._value_color = value_color  # pragma: no mutate
        self._fields = fields  # pragma: no mutate
        self._fields_set = frozenset(fields)  # pragma: no mutate

    def __call__(self, logger: object, name: str, event_dict: dict[str, Any]) -> str:  # noqa: ARG002  # pragma: no mutate
        parts: list[str] = []

        # 1. Timestamp
        ts = event_dict.pop("timestamp", None)
        if ts is not None:
            ts_str = str(ts)
            if self._colors:
                parts.append(DIM + ts_str + RESET)  # pragma: no mutate
            else:
                parts.append(ts_str)

        # 2. Level
        level = event_dict.pop("level", "")  # pragma: no mutate
        level_str = str(level).lower()
        padded = level_str.ljust(_LEVEL_PAD)  # pragma: no mutate
        if self._colors:
            color = LEVEL_COLORS.get(level_str, "")  # pragma: no mutate
            parts.append("[" + color + padded + RESET + "]")  # pragma: no mutate
        else:
            parts.append("[" + padded + "]")  # pragma: no mutate

        # 3. Event / message body
        event = event_dict.pop("event", "")  # pragma: no mutate
        parts.append(str(event))

        # 4. Remaining keys — sorted, optionally filtered key=repr(value) pairs
        fields_set = self._fields_set
        filtered_items = [(k, event_dict[k]) for k in sorted(event_dict) if not fields_set or k in fields_set]
        for key, val in filtered_items:
            val_repr = repr(val)
            key_part = self._key_color + key + RESET if self._colors and self._key_color else key  # pragma: no mutate
            val_part = (
                self._value_color + val_repr + RESET if self._colors and self._value_color else val_repr
            )  # pragma: no mutate
            parts.append(key_part + "=" + val_part)  # pragma: no mutate

        return " ".join(parts)  # pragma: no mutate
