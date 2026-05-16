#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unified color-mode dispatcher — handles str or bytes transparently."""

from __future__ import annotations

from typing import Literal, overload

from provide.uterm.colors.downgrade import downgrade_to_16, downgrade_to_256

ColorMode = Literal["passthrough", "256", "16"]


@overload
def apply_color_mode(data: bytes, mode: ColorMode) -> bytes: ...
@overload
def apply_color_mode(data: str, mode: ColorMode) -> str: ...


def apply_color_mode(data: bytes | str, mode: ColorMode) -> bytes | str:
    """Apply a color-mode filter to data, preserving the input type.

    Accepts either :class:`str` or :class:`bytes`. Bytes are decoded as
    latin-1 for the regex pass and re-encoded on return so stream filters
    (e.g. gateway writers) and text renderers share the same
    implementation.

    Args:
        data: ANSI text or raw bytes containing SGR sequences.
        mode:
            ``"passthrough"``: return data unchanged (zero-cost hot path).
            ``"256"``: downgrade truecolor SGR to xterm-256 cube.
            ``"16"``: downgrade truecolor SGR to the base 16-color palette.

    Returns:
        Filtered data, same type as the input.
    """
    if mode == "passthrough":
        return data
    if isinstance(data, bytes):
        text = data.decode("latin-1", errors="replace")
        text = downgrade_to_256(text) if mode == "256" else downgrade_to_16(text)
        return text.encode("latin-1", errors="replace")
    return downgrade_to_256(data) if mode == "256" else downgrade_to_16(data)


__all__ = ["ColorMode", "apply_color_mode"]
