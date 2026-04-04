#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Heuristic input-type detection from terminal screen text."""

from __future__ import annotations


def auto_detect_input_type(screen: str) -> str:
    """Heuristically detect input type from prompt text.

    Args:
        screen: Screen text to analyze

    Returns:
        "any_key", "single_key", or "multi_key"
    """
    screen_lower = screen.lower()

    if any(
        phrase in screen_lower
        for phrase in [
            "press any key",
            "press a key",
            "hit any key",
            "strike any key",
            "<more>",
            "[more]",
            "-- more --",
        ]
    ):
        return "any_key"

    if any(
        phrase in screen_lower
        for phrase in [
            "(y/n)",
            "(yes/no)",
            "continue?",
            "quit?",
            "abort?",
            "retry?",
            "[y/n]",
            "(q)uit",
            "(a)bort",
        ]
    ):
        return "single_key"

    if any(
        phrase in screen_lower
        for phrase in [
            "enter",
            "type",
            "input",
            "name:",
            "password:",
            "username:",
            "choose:",
            "select:",
            "command:",
            "search:",
        ]
    ):
        return "multi_key"

    return "multi_key"
