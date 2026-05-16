#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for character-level input filters (consume_iac, consume_escape)."""

from __future__ import annotations

from collections import deque

from provide.uterm.filters import (
    DO,
    DONT,
    IAC,
    SB,
    SE,
    WILL,
    WONT,
    ByteReader,
    consume_escape,
    consume_iac,
)


class MockReader:
    """ByteReader backed by a deque of bytes."""

    def __init__(self, data: bytes) -> None:
        self._queue: deque[int] = deque(data)

    async def read(self, n: int) -> bytes:
        result = bytearray()
        for _ in range(n):
            if self._queue:
                result.append(self._queue.popleft())
            else:
                break
        return bytes(result)


# ---------------------------------------------------------------------------
# ByteReader protocol
# ---------------------------------------------------------------------------


def test_mock_reader_implements_protocol() -> None:
    assert isinstance(MockReader(b""), ByteReader)


# ---------------------------------------------------------------------------
# consume_iac
# ---------------------------------------------------------------------------


class TestConsumeIac:
    """Tests for telnet IAC command consumption."""

    async def test_will_command(self) -> None:
        """WILL + option byte → both consumed."""
        reader = MockReader(bytes([WILL, 0x01]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_wont_command(self) -> None:
        reader = MockReader(bytes([WONT, 0x03]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_do_command(self) -> None:
        reader = MockReader(bytes([DO, 0x18]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_dont_command(self) -> None:
        reader = MockReader(bytes([DONT, 0x18]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_sub_negotiation(self) -> None:
        """SB ... IAC SE → entire sub-negotiation consumed."""
        reader = MockReader(bytes([SB, 0x18, 0x00, IAC, SE]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_sub_negotiation_empty_read(self) -> None:
        """SB with premature EOF → returns without error."""
        reader = MockReader(bytes([SB]))
        await consume_iac(reader)
        # exhausted — no crash

    async def test_sub_negotiation_iac_not_se(self) -> None:
        """SB ... IAC (not SE) continues loop until IAC SE."""
        reader = MockReader(bytes([SB, 0x01, IAC, 0x02, IAC, SE]))
        await consume_iac(reader)
        assert len(reader._queue) == 0

    async def test_iac_iac_escaped(self) -> None:
        """IAC IAC (escaped 0xFF) → consumed, trailing data remains."""
        reader = MockReader(bytes([IAC, 0x41]))
        await consume_iac(reader)
        # IAC consumed as cmd byte, 0x41 is not WILL/WONT/DO/DONT/SB
        # so it falls through — but cmd byte already consumed
        assert len(reader._queue) == 1  # 0x41 was read as part of fallthrough

    async def test_empty_read_returns(self) -> None:
        """Empty read after IAC → returns immediately."""
        reader = MockReader(b"")
        await consume_iac(reader)
        # no crash

    async def test_sub_negotiation_iac_eof_after_iac(self) -> None:
        """SB ... IAC then EOF → returns."""
        reader = MockReader(bytes([SB, 0x01, IAC]))
        await consume_iac(reader)
        # se read returns empty → returns


# ---------------------------------------------------------------------------
# consume_escape
# ---------------------------------------------------------------------------


class TestConsumeEscape:
    """Tests for ANSI escape sequence consumption."""

    async def test_csi_arrow_key(self) -> None:
        """ESC [ A (up arrow) → consumed."""
        reader = MockReader(bytes([0x5B, 0x41]))  # [ A
        await consume_escape(reader)
        assert len(reader._queue) == 0

    async def test_csi_with_params(self) -> None:
        """ESC [ 1 ; 5 C (Ctrl+Right) → consumed."""
        reader = MockReader(bytes([0x5B, 0x31, 0x3B, 0x35, 0x43]))  # [ 1 ; 5 C
        await consume_escape(reader)
        assert len(reader._queue) == 0

    async def test_csi_empty_read(self) -> None:
        """CSI then EOF → returns without error."""
        reader = MockReader(bytes([0x5B]))  # [ then EOF
        await consume_escape(reader)

    async def test_ss3_sequence(self) -> None:
        """ESC O P (F1 key) → consumed."""
        reader = MockReader(bytes([0x4F, 0x50]))  # O P
        await consume_escape(reader)
        assert len(reader._queue) == 0

    async def test_two_char_escape(self) -> None:
        """ESC + letter (e.g. Alt+a) → consumed."""
        reader = MockReader(bytes([0x61]))  # 'a'
        await consume_escape(reader)
        assert len(reader._queue) == 0

    async def test_empty_read_returns(self) -> None:
        """Empty read after ESC → returns immediately."""
        reader = MockReader(b"")
        await consume_escape(reader)
