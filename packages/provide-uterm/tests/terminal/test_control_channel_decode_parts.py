#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Direct coverage for ``ControlFrameDecoder._decode_data_parts``.

Two of its four branches are unreachable through ``feed()``: the decoder only
ever appends a ``DataChunk`` when it has at least one part, and it always builds
those parts as ``memoryview`` slices over the input. The empty-parts and
single-``bytes``-part arms exist because the signature accepts
``Sequence[memoryview | bytes]``, so they are exercised here directly rather
than through the public API.

Deliberately NOT placed in test_control_channel_codec.py / _coverage.py /
_mutmut.py: those are wired into ``[tool.mutmut]``'s test selection, and editing
a mutation support file with no perimeter-source change forces a full-perimeter
mutmut run. These are coverage tests, not kill-suite tests.
"""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import ControlFrameDecoder

_decode = ControlFrameDecoder._decode_data_parts


class TestDecodeDataParts:
    def test_empty_parts_decodes_to_empty_string(self) -> None:
        """No parts is not an error -- it decodes to the empty string."""
        assert _decode([]) == ""

    def test_single_bytes_part_decodes_without_a_memoryview_hop(self) -> None:
        """A lone ``bytes`` part takes the direct .decode() arm."""
        assert _decode([b"hello"]) == "hello"

    def test_single_memoryview_part_decodes_via_tobytes(self) -> None:
        assert _decode([memoryview(b"hello")]) == "hello"

    @pytest.mark.parametrize(
        ("parts", "expected"),
        [
            ([b"ab", b"cd"], "abcd"),
            ([memoryview(b"ab"), memoryview(b"cd")], "abcd"),
            ([memoryview(b"ab"), b"cd"], "abcd"),
        ],
        ids=["bytes", "memoryviews", "mixed"],
    )
    def test_multiple_parts_are_concatenated_before_decoding(
        self, parts: list[memoryview | bytes], expected: str
    ) -> None:
        assert _decode(parts) == expected

    def test_multibyte_codepoint_split_across_parts(self) -> None:
        """The merge-then-decode order matters: neither half is valid UTF-8 alone."""
        snowman = "☃".encode()
        assert _decode([snowman[:1], snowman[1:]]) == "☃"
