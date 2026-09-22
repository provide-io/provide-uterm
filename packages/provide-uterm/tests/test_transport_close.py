#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A closed transport says who closed it, and the session keeps that answer.

Before this contract every transport reported the end of a connection as a
bare ``ConnectionError`` whose only content was a transport-specific message
("Connection closed", "Connection lost", ...), and ``TransportSession``'s reader
discarded even that. A client-side keepalive timeout and a server closing the
socket were indistinguishable: live 2026-09-13, a Cloudflare Durable Object
session dropped after 20-30s of silence and nothing could say which side ended it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from provide.uterm.transport_close import CloseInitiator, TransportClose, TransportClosedError
from provide.uterm.transport_session import TransportSession


def test_the_closed_error_is_a_connection_error_carrying_the_close() -> None:
    close = TransportClose(CloseInitiator.LOCAL, code=1011, reason="keepalive ping timeout")

    err = TransportClosedError("Connection closed", close)

    assert isinstance(err, ConnectionError)
    assert err.close is close
    assert str(err) == "Connection closed (local close 1011 keepalive ping timeout)"


def _close_cases() -> dict[str, Any]:
    """``close_cases`` from the shared vectors every port is tested against."""
    here = Path(__file__).resolve()
    for path in (
        here.parents[3] / "spec" / "behavior_vectors.json",  # repo root
        here.parents[4] / "spec" / "behavior_vectors.json",  # mutants/<root>
        here.parent / "bridge" / "testdata" / "behavior_vectors.json",
    ):
        if path.is_file():
            cases: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["close_cases"]
            return cases
    raise FileNotFoundError("behavior_vectors.json not found")


@pytest.mark.parametrize("case", _close_cases()["summary"], ids=lambda case: case["summary"])
def test_the_summary_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    close = TransportClose(
        CloseInitiator(case["initiator"]), code=case["code"], reason=case["reason"], detail=case["detail"]
    )

    assert close.summary() == case["summary"]


class _EndsWith:
    """Transport that yields one chunk, then raises *exc* on every later read."""

    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc
        self.receives = 0

    async def connect(self, *a: Any, **kw: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        return None

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        self.receives += 1
        if self.receives == 1:
            return b"hello"
        if self._exc is None:
            await asyncio.sleep(0.01)
            return b""
        raise self._exc

    def is_connected(self) -> bool:
        return True


class _Session(TransportSession):
    async def _connect_transport(self) -> None:
        await self._transport.connect()


async def _until_disconnected(session: _Session) -> None:
    for _ in range(200):
        if not session.is_connected():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the reader never noticed the transport close")


def test_a_new_session_has_no_close() -> None:
    assert _Session(_EndsWith(None), cols=80, rows=25).close_info is None


async def test_the_reader_keeps_the_transport_close() -> None:
    close = TransportClose(CloseInitiator.REMOTE, code=1001, reason="going away")
    session = _Session(_EndsWith(TransportClosedError("Connection closed", close)), cols=80, rows=25)

    await session.connect()
    await _until_disconnected(session)

    assert session.close_info == close


async def test_an_untyped_drop_is_an_unknown_close_with_its_detail() -> None:
    session = _Session(_EndsWith(ConnectionResetError("peer reset")), cols=80, rows=25)

    await session.connect()
    await _until_disconnected(session)

    assert session.close_info is not None
    assert session.close_info.initiator is CloseInitiator.UNKNOWN
    assert session.close_info.detail == "ConnectionResetError: peer reset"


async def test_closing_the_session_is_a_local_close() -> None:
    session = _Session(_EndsWith(None), cols=80, rows=25)
    await session.connect()

    await session.close()

    assert session.close_info is not None
    assert session.close_info.initiator is CloseInitiator.LOCAL


async def test_closing_after_the_transport_closed_keeps_the_transport_close() -> None:
    close = TransportClose(CloseInitiator.REMOTE, code=1001, reason="going away")
    session = _Session(_EndsWith(TransportClosedError("Connection closed", close)), cols=80, rows=25)
    await session.connect()
    await _until_disconnected(session)

    await session.close()

    assert session.close_info == close
