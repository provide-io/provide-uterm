#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""One shared character cap preserves terminal data through bounded event rings."""

import pytest

from provide.uterm import control_channel
from provide.uterm.defaults import TerminalDefaults


@pytest.mark.parametrize("size", [0, 1, 8191, 8192, 8193, 32769])
def test_default_chunks_preserve_text_within_event_cap(size: int) -> None:
    text = ("\x1b[31m█😀\x10\x02\r\n" * (size + 1))[:size]
    chunks = list(control_channel.terminal_data_chunks(text))
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= TerminalDefaults.MAX_EVENT_DATA_CHARS for chunk in chunks)


def test_smaller_ring_policy_is_shared() -> None:
    assert list(control_channel.terminal_data_chunks("abcdefg", max_chars=3)) == ["abc", "def", "g"]


@pytest.mark.parametrize("cap", [0, -1])
def test_nonpositive_cap_is_rejected_even_for_empty_data(cap: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        list(control_channel.terminal_data_chunks("", max_chars=cap))
