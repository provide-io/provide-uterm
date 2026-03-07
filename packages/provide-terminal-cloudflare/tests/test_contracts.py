from __future__ import annotations

import pytest
from provide_terminal_cloudflare.contracts import MessageLimits, ProtocolError, parse_frame


def test_parse_input_frame_ok() -> None:
    frame = parse_frame('{"type":"input","data":"hello"}')
    assert frame["type"] == "input"
    assert frame["data"] == "hello"


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_frame("{")


def test_parse_large_input_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_frame('{"type":"input","data":"abcd"}', limits=MessageLimits(max_input_chars=2))
