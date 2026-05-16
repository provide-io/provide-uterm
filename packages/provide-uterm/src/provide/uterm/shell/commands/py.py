#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``py`` command — evaluate a Python expression in the shell sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.uterm.shell._output import PROMPT, error_msg, success_msg

if TYPE_CHECKING:
    from provide.uterm.shell._sandbox import Sandbox


async def cmd_py(sandbox: Sandbox, source: str) -> list[str]:
    """Evaluate *source* in *sandbox* and return rendered output frames."""
    if not source:
        return [error_msg("usage: py <expr>") + PROMPT]
    result = sandbox.run(source)
    output = result if result else success_msg("ok")
    return [output + PROMPT]
