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

from provide.uterm.transport_session import TransportSession

from .test_transport_session import _ConcreteSession, _FakeTransport

# ---------------------------------------------------------------------------
# control_frames: off by default (raw passthrough), opt-in DLE/STX parsing
# ---------------------------------------------------------------------------


def _control_frame_bytes(payload: dict[str, Any]) -> bytes:
    from provide.uterm.control_channel import encode_control_frame

    return encode_control_frame(payload).encode("cp437")


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
