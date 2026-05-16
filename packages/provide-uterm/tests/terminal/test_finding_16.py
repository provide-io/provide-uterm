#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest

from provide.uterm.line_editor import LineEditor


@pytest.mark.asyncio
async def test_line_editor_bell_on_max_length():
    written = []

    async def on_write(data):
        written.append(data)

    editor = LineEditor(max_length=3, on_write=on_write)

    await editor.process_char("a")
    await editor.process_char("b")
    await editor.process_char("c")
    assert len(editor.buffer) == 3

    written.clear()
    await editor.process_char("d")
    assert len(editor.buffer) == 3
    assert "\a" in written
