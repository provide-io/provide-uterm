from __future__ import annotations

import pytest
from provide.uterm.cloudflare.contracts import MessageLimits, ProtocolError, frame_json, parse_frame, parse_stream


def test_parse_input_frame_ok() -> None:
    frame = parse_frame(frame_json("input", data="hello"), data_frame_type="input")
    assert frame["type"] == "input"
    assert frame["data"] == "hello"


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_frame("\x10\x02ZZZZZZZZ:", data_frame_type="input")


def test_parse_large_input_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_frame(frame_json("input", data="abcd"), data_frame_type="input", limits=MessageLimits(max_input_chars=2))


def test_parse_message_too_large_raises() -> None:
    """Line 156: message byte size > max_ws_message_bytes → ProtocolError."""
    big = frame_json("input", data="x" * 200)
    with pytest.raises(ProtocolError, match="message too large"):
        parse_frame(big, data_frame_type="input", limits=MessageLimits(max_ws_message_bytes=10))


def test_parse_non_dict_raises() -> None:
    """Control payloads must be JSON objects."""
    with pytest.raises(ProtocolError, match="control payload must be an object"):
        parse_frame("\x10\x0200000007:[1,2,3]")


def test_parse_missing_type_raises() -> None:
    """Control payloads without a type are rejected."""
    with pytest.raises(ProtocolError, match="missing frame type"):
        parse_frame('\x10\x0200000010:{"data":"hello"}')


def test_parse_snapshot_frame() -> None:
    """Line 175: snapshot frame captures screen."""
    frame = parse_frame(frame_json("snapshot", screen="hello world"))
    assert frame["type"] == "snapshot"
    assert frame["screen"] == "hello world"


def test_parse_term_frame() -> None:
    """Line 177: term frame captures data."""
    frame = parse_frame(frame_json("term", data="output"), data_frame_type="term")
    assert frame["type"] == "term"
    assert frame["data"] == "output"


def test_parse_control_frame_with_owner() -> None:
    """Lines 179-180: control frame captures action and owner."""
    frame = parse_frame(frame_json("control", action="pause", owner="alice"))
    assert frame["action"] == "pause"
    assert frame["owner"] == "alice"


def test_parse_control_frame_no_owner() -> None:
    """Line 180: control frame with no owner → owner=None."""
    frame = parse_frame(frame_json("control", action="resume"))
    assert frame["owner"] is None


def test_parse_analysis_frame() -> None:
    """Line 182: analysis frame captures formatted."""
    frame = parse_frame(frame_json("analysis", formatted="some text"))
    assert frame["formatted"] == "some text"


def test_parse_hijack_state_frame() -> None:
    """Lines 184-187: hijack_state frame captures hijacked/owner/lease_expires_at."""
    frame = parse_frame(frame_json("hijack_state", hijacked=True, owner="bob", lease_expires_at=9999.0))
    assert frame["hijacked"] is True
    assert frame["owner"] == "bob"
    assert frame["lease_expires_at"] == 9999.0


def test_parse_hijack_state_no_lease() -> None:
    """Line 187: hijack_state with no lease_expires_at → None."""
    frame = parse_frame(frame_json("hijack_state", hijacked=False))
    assert frame["lease_expires_at"] is None


def test_parse_worker_hello_hijack_mode() -> None:
    """Lines 188-191: worker_hello frame with input_mode=hijack captures mode."""
    frame = parse_frame(frame_json("worker_hello", input_mode="hijack"))
    assert frame.get("mode") == "hijack"


def test_parse_worker_hello_open_mode() -> None:
    """Lines 188-191: worker_hello frame with input_mode=open captures mode."""
    frame = parse_frame(frame_json("worker_hello", input_mode="open"))
    assert frame.get("mode") == "open"


def test_parse_worker_hello_invalid_mode_no_mode_field() -> None:
    """worker_hello with unsupported mode → mode field not set."""
    frame = parse_frame(frame_json("worker_hello", input_mode="invalid"))
    assert "mode" not in frame


def test_parse_worker_hello_with_protocol_version_int() -> None:
    """Lines 99-101: worker_hello with numeric protocol_version is captured as int."""
    frame = parse_frame(frame_json("worker_hello", input_mode="open", protocol_version=3))
    assert frame.get("protocol_version") == 3


def test_parse_worker_hello_with_invalid_protocol_version_silently_ignored() -> None:
    """Lines 102-103: worker_hello with non-numeric protocol_version is dropped silently."""
    frame = parse_frame(frame_json("worker_hello", input_mode="open", protocol_version="not-a-number"))
    assert "protocol_version" not in frame


def test_parse_passthrough_frames() -> None:
    """Lines 192-204: snapshot_req/error/heartbeat/ping/hijack_request etc. pass through."""
    for frame_type in (
        "snapshot_req",
        "error",
        "heartbeat",
        "ping",
        "hijack_request",
        "hijack_release",
        "hijack_step",
        "hello",
        "worker_connected",
        "worker_disconnected",
    ):
        frame = parse_frame(frame_json(frame_type))
        assert frame["type"] == frame_type


def test_parse_unsupported_frame_type_raises() -> None:
    """Line 206: unknown frame type → ProtocolError."""
    with pytest.raises(ProtocolError, match="unsupported frame type"):
        parse_frame('\x10\x020000001d:{"type":"completely_unknown"}')


def test_frame_json_produces_valid_frame() -> None:
    """frame_json returns an encoded control frame with the correct payload."""
    raw = frame_json("heartbeat")
    data = parse_frame(raw)
    assert data["type"] == "heartbeat"
    assert "ts" in data


def test_frame_json_with_kwargs() -> None:
    """frame_json passes extra kwargs into the encoded payload."""
    raw = frame_json("control", action="pause", owner="alice")
    data = parse_frame(raw)
    assert data["action"] == "pause"
    assert data["owner"] == "alice"


def test_parse_stream_empty_raises() -> None:
    """parse_stream on empty input raises ProtocolError('empty frame')."""
    with pytest.raises(ProtocolError, match="empty frame"):
        parse_stream("", data_frame_type="input")


def test_parse_frame_no_type_term_data_raises() -> None:
    """parse_frame without data_frame_type raises when stream yields a 'term' data frame."""
    raw = frame_json("term", data="output")
    with pytest.raises(ProtocolError, match="data frame type required"):
        parse_frame(raw)


def test_parse_frame_multiple_frames_raises() -> None:
    """parse_frame raises when the raw string contains multiple frames."""
    raw = frame_json("heartbeat") + frame_json("heartbeat")
    with pytest.raises(ProtocolError, match="expected a single frame"):
        parse_frame(raw, data_frame_type="input")
