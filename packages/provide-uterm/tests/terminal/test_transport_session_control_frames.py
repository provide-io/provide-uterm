#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for TransportSession's opt-in control_frames mode.

Split out of test_transport_session.py to stay under the max-LOC cap; reuses
that module's _FakeTransport/_ConcreteSession fixtures.
"""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.transport_session import TerminalCapture, TransportSession

from .test_transport_session import _ConcreteSession, _FakeTransport

# ---------------------------------------------------------------------------
# control_frames: off by default (raw passthrough), opt-in DLE/STX parsing
# ---------------------------------------------------------------------------


def _control_frame_bytes(payload: dict[str, Any]) -> bytes:
    from provide.uterm.control_channel import encode_control_frame

    return encode_control_frame(payload).encode("cp437")


def _utf8_control_frame_bytes(payload: dict[str, Any]) -> bytes:
    from provide.uterm.control_channel import encode_control_frame

    return encode_control_frame(payload).encode("utf-8")


async def test_control_frames_off_by_default_is_raw_passthrough() -> None:
    """Default behavior: a DLE/STX-framed message is NOT parsed — the exact
    unmodified bytes reach watchers/the emulator, exactly like a plain
    (non-uterm-aware) client that has never heard of the control channel."""
    frame = _control_frame_bytes({"type": "render_speed", "cps": 2400})
    chunk = frame + b"hi"
    transport = _FakeTransport([chunk, ConnectionResetError("done")])
    session = _ConcreteSession(transport)
    assert session._control_decoder is None

    raw_seen: list[bytes] = []
    session.add_watch(lambda _state, raw: raw_seen.append(raw))

    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert raw_seen == [chunk]


async def test_control_frames_enabled_routes_control_chunks_to_watcher() -> None:
    """With control_frames=True, a DLE/STX frame is parsed out and dispatched
    to control-frame watchers instead of reaching the emulator/screen."""
    frame = _control_frame_bytes({"type": "render_speed", "cps": 2400})
    transport = _FakeTransport([frame + b"hi", ConnectionResetError("done")])
    session = _ConcreteSession(transport, control_frames=True)
    assert session._control_decoder is not None

    seen: list[dict[str, Any]] = []
    session.add_control_frame_watch(seen.append)

    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert seen == [{"type": "render_speed", "cps": 2400}]
    snap = session.snapshot()["screen"]
    assert "hi" in snap
    assert "render_speed" not in snap


async def test_control_frames_enabled_pure_text_chunk_unaffected() -> None:
    """A chunk with no control frame at all still reaches the emulator/watchers
    normally when control_frames=True — the decoder is transparent for it."""
    transport = _FakeTransport([b"plain text", ConnectionResetError("done")])
    session = _ConcreteSession(transport, control_frames=True)

    raw_seen: list[bytes] = []
    session.add_watch(lambda _state, raw: raw_seen.append(raw))

    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert raw_seen == [b"plain text"]
    assert "plain text" in session.snapshot()["screen"]


async def test_control_frames_enabled_chunk_with_only_control_frame() -> None:
    """A read containing ONLY a control frame (no trailing text) must not feed
    an empty chunk to the emulator/watchers (no phantom screen-change tick)."""
    frame = _control_frame_bytes({"type": "ping"})
    transport = _FakeTransport([frame, b"later", ConnectionResetError("done")])
    session = _ConcreteSession(transport, control_frames=True)

    raw_seen: list[bytes] = []
    session.add_watch(lambda _state, raw: raw_seen.append(raw))
    control_seen: list[dict[str, Any]] = []
    session.add_control_frame_watch(control_seen.append)

    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert control_seen == [{"type": "ping"}]
    # Only the "later" chunk ever reached watchers/emulator — the control-only
    # read produced no data chunk.
    assert raw_seen == [b"later"]
    assert "later" in session.snapshot()["screen"]


def test_control_frame_watch_noop_when_disabled() -> None:
    """Registering a control-frame watcher on a control_frames=False session
    is harmless — there is simply nothing that will ever call it."""
    session = TransportSession(_FakeTransport())
    session.add_control_frame_watch(lambda _payload: None)
    assert len(session._control_watchers) == 1
    assert session._control_decoder is None


async def test_operation_capture_excludes_control_frames_and_decodes_target_encoding() -> None:
    banner = "╔══ WARP ══╗"
    frame = _utf8_control_frame_bytes({"type": "online_presence", "count": 1})
    transport = _FakeTransport([frame + banner.encode("utf-8"), ConnectionResetError("done")])
    session = _ConcreteSession(transport, receive_encoding="utf-8", control_frames=True)

    with session.capture_output() as capture:
        await session.connect()
        await asyncio.sleep(0.1)

    await session.close()

    assert capture.text == banner
    assert "online_presence" not in capture.text
    assert "Γò" not in capture.text


def test_terminal_capture_append_ignores_empty_text() -> None:
    """An empty append is a no-op and must not disturb what is already held.

    The reader loop cannot currently deliver this: `_split_control_frames`
    returns None (not empty bytes) when a payload was frames-only, and non-empty
    bytes always decode to a non-empty string under errors="replace". The guard
    in `_append` is therefore defensive, and this exercises it directly rather
    than through a session that cannot reach it.
    """
    capture = TerminalCapture(max_chars=8)

    capture._append("")
    assert capture.text == ""

    capture._append("abc")
    capture._append("")
    assert capture.text == "abc"


def test_terminal_capture_floors_max_chars() -> None:
    """max_chars below 1 is floored, so a capture always retains something."""
    capture = TerminalCapture(max_chars=0)
    capture._append("abcdef")
    assert capture.text == "f"


async def test_operation_capture_ignores_frame_only_payload() -> None:
    """A frame-only payload leaves no terminal text, so the capture stays empty.

    Distinct from the unit test above: here the reader loop short-circuits on
    `_split_control_frames` returning None, so the capture is never appended to
    at all.
    """
    frame = _utf8_control_frame_bytes({"type": "online_presence", "count": 1})
    transport = _FakeTransport([frame, ConnectionResetError("done")])
    session = _ConcreteSession(transport, receive_encoding="utf-8", control_frames=True)

    with session.capture_output() as capture:
        await session.connect()
        await asyncio.sleep(0.1)

    await session.close()

    assert capture.text == ""


async def test_operation_capture_is_bounded() -> None:
    transport = _FakeTransport([b"abcdefgh", ConnectionResetError("done")])
    session = _ConcreteSession(transport)

    with session.capture_output(max_chars=5) as capture:
        await session.connect()
        await asyncio.sleep(0.1)

    await session.close()

    assert capture.text == "defgh"


async def test_operation_capture_stops_recording_when_scope_exits() -> None:
    class _GatedTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.release_second = asyncio.Event()

        async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
            del max_bytes, timeout_ms
            if self._idx == 0:
                self._idx += 1
                return b"during"
            if self._idx == 1:
                self._idx += 1
                await self.release_second.wait()
                return b"after"
            raise ConnectionResetError("done")

    transport = _GatedTransport()
    session = _ConcreteSession(transport)

    with session.capture_output() as capture:
        await session.connect()
        while session.screen_change_seq() < 1:
            await asyncio.sleep(0)

    transport.release_second.set()
    await asyncio.sleep(0.1)
    await session.close()

    assert capture.text == "during"
