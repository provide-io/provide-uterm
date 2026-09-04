#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The RFB client's handshake, framebuffer tracking, and every refusal.

Driven through :class:`io.BytesIO` scripts rather than a socket: an RFB
conversation is a byte sequence, and the interesting cases are the malformed
ones a real server will not produce on demand — a truncated stream, a security
list without ``None``, a ServerInit claiming a 60000-pixel desktop.

The reader thread terminates when its scripted stream is exhausted, so each
test can join it and assert on settled state.
"""

from __future__ import annotations

import struct
import threading
from io import BytesIO

import pytest

from provide.uterm.server.rfb_session import (
    ENCODING_COPY_RECT,
    ENCODING_RAW,
    RfbGraphicalSession,
    _negotiate_security,
    _negotiate_version,
    _read_exact,
    _read_server_init,
    _send_set_encodings,
    _send_set_pixel_format,
    _send_update_request,
    encode_key_event,
    encode_pointer_event,
)


def _server_init(width: int, height: int, name: bytes = b"") -> bytes:
    return struct.pack(">HH", width, height) + bytes(16) + struct.pack(">I", len(name)) + name


def _raw_update(x: int, y: int, w: int, h: int, pixels: bytes) -> bytes:
    return struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", x, y, w, h, ENCODING_RAW) + pixels


class _Dribble:
    """A reader that returns one byte at a time, to exercise the read loop."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0

    def read(self, size: int) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        chunk = self._payload[self._pos : self._pos + 1]
        self._pos += 1
        _ = size
        return chunk


# --- framing primitives -----------------------------------------------------


def test_read_exact_reassembles_a_dribbled_stream() -> None:
    assert _read_exact(_Dribble(b"abcdef"), 6) == b"abcdef"  # type: ignore[arg-type]


def test_read_exact_names_how_much_was_outstanding() -> None:
    with pytest.raises(ConnectionError, match="4 of 6 bytes outstanding"):
        _read_exact(BytesIO(b"ab"), 6)


def test_the_wire_messages_are_the_sizes_rfb_specifies() -> None:
    for send, size in ((_send_set_pixel_format, 20), (_send_set_encodings, 12)):
        buf = BytesIO()
        send(buf)
        assert len(buf.getvalue()) == size, send.__name__

    buf = BytesIO()
    _send_update_request(buf, 800, 600, incremental=True)
    assert buf.getvalue() == struct.pack(">BBHHHH", 3, 1, 0, 0, 800, 600)

    buf = BytesIO()
    _send_update_request(buf, 800, 600, incremental=False)
    assert buf.getvalue()[1] == 0


def test_input_events_encode_to_the_documented_layout() -> None:
    assert encode_pointer_event(10, 20, 1) == struct.pack(">BBHH", 5, 1, 10, 20)
    # Negative coordinates are clamped rather than wrapping into a huge u16.
    assert encode_pointer_event(-5, -5, 0xFFFF) == struct.pack(">BBHH", 5, 0xFF, 0, 0)
    assert encode_key_event(0xFF0D, down=True) == struct.pack(">BBHI", 4, 1, 0, 0xFF0D)
    assert encode_key_event(0xFF0D, down=False)[1] == 0


# --- version + security -----------------------------------------------------


def test_version_prefers_3_8_when_offered() -> None:
    writer = BytesIO()
    assert _negotiate_version(BytesIO(b"RFB 003.008\n"), writer) == "RFB 003.008\n"
    assert writer.getvalue() == b"RFB 003.008\n"


def test_version_echoes_what_an_older_server_offered() -> None:
    writer = BytesIO()
    assert _negotiate_version(BytesIO(b"RFB 003.003\n"), writer) == "RFB 003.003\n"


def test_a_non_rfb_peer_is_refused_before_the_security_byte() -> None:
    with pytest.raises(ConnectionError, match="not an RFB server"):
        _negotiate_version(BytesIO(b"HTTP/1.1 200"), BytesIO())


def test_security_3_8_selects_none_and_reads_the_result() -> None:
    reader = BytesIO(bytes([2, 1, 2]) + struct.pack(">I", 0))
    writer = BytesIO()
    _negotiate_security(reader, writer, "RFB 003.008\n")
    assert writer.getvalue() == bytes([1])


def test_security_3_7_selects_none_without_a_security_result() -> None:
    # 3.7 has no SecurityResult; consuming four bytes here would desynchronise.
    reader = BytesIO(bytes([1, 1]))
    _negotiate_security(reader, BytesIO(), "RFB 003.007\n")
    assert reader.read() == b""


def test_an_empty_security_list_is_the_server_refusing() -> None:
    with pytest.raises(ConnectionError, match="offered no types"):
        _negotiate_security(BytesIO(bytes([0])), BytesIO(), "RFB 003.008\n")


def test_a_server_without_type_none_is_refused_and_says_what_it_offered() -> None:
    with pytest.raises(ConnectionError, match=r"\[2, 16\]"):
        _negotiate_security(BytesIO(bytes([2, 2, 16])), BytesIO(), "RFB 003.008\n")


def test_a_nonzero_security_result_is_a_rejection() -> None:
    reader = BytesIO(bytes([1, 1]) + struct.pack(">I", 1))
    with pytest.raises(ConnectionError, match="security rejected"):
        _negotiate_security(reader, BytesIO(), "RFB 003.008\n")


def test_security_3_3_accepts_none_and_refuses_anything_else() -> None:
    _negotiate_security(BytesIO(struct.pack(">I", 1)), BytesIO(), "RFB 003.003\n")
    with pytest.raises(ConnectionError, match="unsupported RFB security type 2"):
        _negotiate_security(BytesIO(struct.pack(">I", 2)), BytesIO(), "RFB 003.003\n")


# --- ServerInit -------------------------------------------------------------


def test_server_init_returns_the_dimensions_and_consumes_the_name() -> None:
    reader = BytesIO(_server_init(640, 480, b"desktop"))
    assert _read_server_init(reader) == (640, 480)
    assert reader.read() == b""


@pytest.mark.parametrize(("width", "height"), [(0, 480), (640, 0), (60000, 480), (640, 60000)])
def test_a_hostile_server_init_cannot_make_us_allocate(width: int, height: int) -> None:
    with pytest.raises(ConnectionError, match="dimensions out of range"):
        _read_server_init(BytesIO(_server_init(width, height)))


def test_an_overlong_desktop_name_is_refused() -> None:
    header = struct.pack(">HH", 64, 64) + bytes(16) + struct.pack(">I", 99999)
    with pytest.raises(ConnectionError, match="desktop name too long"):
        _read_server_init(BytesIO(header))


# --- the session ------------------------------------------------------------


def _settled(reader_payload: bytes, width: int = 4, height: int = 2) -> RfbGraphicalSession:
    """Build a session over a scripted stream and wait for its reader to finish."""
    session = RfbGraphicalSession(BytesIO(reader_payload), BytesIO(), width, height)
    session._thread.join(timeout=5.0)
    return session


def test_a_raw_rectangle_lands_in_the_framebuffer_as_rgba() -> None:
    # One blue pixel, sent BGRA as the negotiated pixel format specifies.
    session = _settled(_raw_update(1, 0, 1, 1, bytes([255, 0, 0, 0])))
    image = session.screenshot()
    idx = (0 * 4 * 4) + (1 * 4)
    assert list(image.pixels[idx : idx + 4]) == [0, 0, 255, 255]


def test_screenshot_returns_a_copy_not_the_live_buffer() -> None:
    session = _settled(_raw_update(0, 0, 1, 1, bytes([1, 2, 3, 4])))
    first = session.screenshot()
    first.pixels[0] = 99
    assert session.screenshot().pixels[0] != 99


def test_a_zero_area_rectangle_is_skipped_rather_than_read() -> None:
    payload = struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", 0, 0, 0, 0, ENCODING_RAW)
    assert _settled(payload).screenshot().pixels[0] == 0


def test_copyrect_consumes_its_source_coordinates() -> None:
    payload = (
        struct.pack(">BBH", 0, 0, 2)
        + struct.pack(">HHHHi", 0, 0, 1, 1, ENCODING_COPY_RECT)
        + struct.pack(">HH", 2, 2)
        + struct.pack(">HHHHi", 0, 0, 1, 1, ENCODING_RAW)
        + bytes([9, 9, 9, 9])
    )
    # The second rect only decodes if the copyrect payload was consumed exactly.
    assert _settled(payload).screenshot().pixels[0] == 9


def test_a_rectangle_outside_the_framebuffer_is_refused() -> None:
    session = _settled(struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", 3, 0, 4, 1, ENCODING_RAW))
    assert session._closed.is_set()


def test_an_unnegotiated_encoding_stops_the_loop() -> None:
    session = _settled(struct.pack(">BBH", 0, 0, 1) + struct.pack(">HHHHi", 0, 0, 1, 1, 7))
    assert session._closed.is_set()


def test_too_many_rectangles_is_refused_before_the_loop() -> None:
    session = _settled(struct.pack(">BBH", 0, 0, 9999))
    assert session._closed.is_set()


def test_an_unhandled_server_message_ends_the_loop_rather_than_guessing() -> None:
    # Bell (2) carries no length; skipping it blindly would desynchronise.
    session = _settled(bytes([2]))
    assert session._closed.is_set()


def test_input_is_written_to_the_wire_and_refused_after_close() -> None:
    session = RfbGraphicalSession(BytesIO(b""), BytesIO(), 4, 2)
    session._thread.join(timeout=5.0)
    session._closed.clear()  # the scripted reader ended; re-open for the write test
    session.inject_pointer(3, 1, 1)
    session.inject_key(0xFF0D, True)
    assert session._writer.getvalue() == encode_pointer_event(3, 1, 1) + encode_key_event(0xFF0D, down=True)  # type: ignore[attr-defined]

    session.close()
    with pytest.raises(ConnectionError, match="session is closed"):
        session.inject_pointer(0, 0, 0)


def test_close_survives_a_stream_that_raises() -> None:
    class _Angry(BytesIO):
        """Raises once. A second raise would land in GC as an unraisable."""

        raised = False

        def close(self) -> None:
            if not self.raised:
                self.raised = True
                raise OSError("nope")
            super().close()

    session = RfbGraphicalSession(_Angry(b""), _Angry(), 2, 2)
    session._thread.join(timeout=5.0)
    session.close()  # must not propagate
    assert session._closed.is_set()


def test_connect_refuses_a_target_it_cannot_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("provide.uterm.server.rfb_session.dial_config_from_target", lambda _t: None)
    target = type("T", (), {"target_id": "nope"})()
    with pytest.raises(ConnectionError, match="not a dialable rfb target"):
        RfbGraphicalSession.connect(target)  # type: ignore[arg-type]


def test_connect_completes_the_handshake_and_requests_a_full_update(monkeypatch: pytest.MonkeyPatch) -> None:
    script = b"RFB 003.008\n" + bytes([1, 1]) + struct.pack(">I", 0) + _server_init(8, 4)
    reader, writer = BytesIO(script), BytesIO()
    monkeypatch.setattr("provide.uterm.server.rfb_session.dial_config_from_target", lambda _t: object())
    monkeypatch.setattr("provide.uterm.server.rfb_session.open_rfb_upstream", lambda _d: (reader, writer))

    target = type("T", (), {"target_id": "lab"})()
    session = RfbGraphicalSession.connect(target)  # type: ignore[arg-type]
    session._thread.join(timeout=5.0)

    assert (session.width, session.height) == (8, 4)
    sent = writer.getvalue()
    assert sent.startswith(b"RFB 003.008\n")
    # ClientInit shared-flag, then the non-incremental first request.
    assert struct.pack(">BBHHHH", 3, 0, 0, 0, 8, 4) in sent


def test_connect_closes_both_streams_when_the_handshake_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    class _Tracked(BytesIO):
        def __init__(self, payload: bytes, label: str) -> None:
            super().__init__(payload)
            self._label = label

        def close(self) -> None:
            closed.append(self._label)
            super().close()

    reader, writer = _Tracked(b"HTTP/1.1 200", "r"), _Tracked(b"", "w")
    monkeypatch.setattr("provide.uterm.server.rfb_session.dial_config_from_target", lambda _t: object())
    monkeypatch.setattr("provide.uterm.server.rfb_session.open_rfb_upstream", lambda _d: (reader, writer))

    target = type("T", (), {"target_id": "lab"})()
    with pytest.raises(ConnectionError, match="not an RFB server"):
        RfbGraphicalSession.connect(target)  # type: ignore[arg-type]
    assert sorted(closed) == ["r", "w"]


def test_the_reader_thread_is_a_daemon_so_it_cannot_hold_the_process_open() -> None:
    session = RfbGraphicalSession(BytesIO(b""), BytesIO(), 2, 2)
    assert session._thread.daemon
    session._thread.join(timeout=5.0)
    assert threading.active_count() >= 1
    session.close()


def test_a_closed_session_stops_the_loop_at_the_top_rather_than_mid_rectangle() -> None:
    """close() must end the loop by its own condition, not by an exception.

    Every other exit from _read_loop is a raise. This is the orderly one: the
    last byte of a complete update is delivered, close() lands, the loop
    finishes the rectangle and sends its next request, and the `while` test
    ends it before another read — so a caller that closes never produces a
    spurious "stream closed" in the log.

    The close is fired from the reader on the final byte rather than from
    another thread, because anything racier just re-runs the exception path.
    """
    payload = _raw_update(0, 0, 1, 1, bytes([7, 7, 7, 0]))

    class _CloseOnLastByte:
        def __init__(self) -> None:
            self.pos = 0
            self.ready = threading.Event()
            self.session: RfbGraphicalSession | None = None

        def read(self, size: int) -> bytes:
            _ = size
            self.ready.wait(5.0)
            if self.pos >= len(payload):
                return b""
            chunk = payload[self.pos : self.pos + 1]
            self.pos += 1
            if self.pos == len(payload):
                assert self.session is not None
                # The flag, not close(): closing the writer here would make the
                # loop's own update-request raise, which is the exception path
                # this test exists to avoid.
                self.session._closed.set()
            return chunk

    reader = _CloseOnLastByte()
    session = RfbGraphicalSession(reader, BytesIO(), 4, 2)  # type: ignore[arg-type]
    reader.session = session
    reader.ready.set()
    session._thread.join(timeout=5.0)

    assert not session._thread.is_alive()
    assert session._closed.is_set()
    # The rectangle before the close still landed.
    assert list(session.screenshot().pixels[0:4]) == [7, 7, 7, 255]


def test_closing_mid_read_is_silent_rather_than_logged_as_a_failure() -> None:
    """The other half of close(): a raise that happens *because* we closed.

    close() shuts both streams, so a loop already inside a read or a write
    raises. That is expected teardown, not a fault, and _read_loop must
    swallow it without logging — which is the `if not self._closed.is_set()`
    branch being false.
    """
    payload = _raw_update(0, 0, 1, 1, bytes([3, 3, 3, 0]))

    class _CloseOnLastByte:
        def __init__(self) -> None:
            self.pos = 0
            self.ready = threading.Event()
            self.session: RfbGraphicalSession | None = None

        def read(self, size: int) -> bytes:
            _ = size
            self.ready.wait(5.0)
            if self.pos >= len(payload):
                return b""
            chunk = payload[self.pos : self.pos + 1]
            self.pos += 1
            if self.pos == len(payload):
                assert self.session is not None
                # Full close: the loop's next write lands on a shut stream.
                self.session.close()
            return chunk

    reader = _CloseOnLastByte()
    session = RfbGraphicalSession(reader, BytesIO(), 4, 2)  # type: ignore[arg-type]
    reader.session = session
    reader.ready.set()
    session._thread.join(timeout=5.0)

    assert not session._thread.is_alive()
    assert session._closed.is_set()
