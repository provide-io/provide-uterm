#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Any, NotRequired, Required, TypedDict

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from provide.terminal.detection.buffer import ScreenBuffer


class ScreenSnapshot(TypedDict):
    """Contract for the snapshot dict passed to process_screen."""

    screen: Required[str]
    screen_hash: Required[str]
    cursor_at_end: NotRequired[bool]
    has_trailing_space: NotRequired[bool]
    cursor: NotRequired[dict[str, int]]
    captured_at: NotRequired[float]


class PromptMatch(BaseModel):
    """A matched prompt pattern with its rule metadata."""

    prompt_id: str
    pattern: dict[str, Any]
    input_type: str
    eol_pattern: str
    kv_extract: list[dict[str, Any]] | dict[str, Any] | None = None


class PromptDetection(BaseModel):
    """Complete prompt detection result."""

    prompt_id: str
    input_type: str
    kv_data: dict[str, Any] = Field(default_factory=dict)
    match: PromptMatch | None = None
    is_idle: bool | None = None
    buffer: ScreenBuffer | None = None


# Resolve the ScreenBuffer forward-reference for Pydantic at runtime.
# ruff TC001 wants the import under TYPE_CHECKING, but Pydantic needs the
# name in module globals to evaluate the annotation string 'ScreenBuffer'.
from provide.terminal.detection.buffer import ScreenBuffer  # noqa: E402, TC001

PromptDetection.model_rebuild()


class PromptDetectionDiagnostics(BaseModel):
    """Detection result with partial-match diagnostics for debugging."""

    match: PromptMatch | None = None
    regex_matched_but_failed: list[dict[str, Any]] = Field(default_factory=list)
