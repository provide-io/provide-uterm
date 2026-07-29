#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the client's control-channel socket.

Terminal bytes and control frames share one socket, so the thing that decides
whether a session works is which of the two a payload becomes.

* **A logical frame is encoded by its type.** `input` and `term` are terminal
  bytes and are escaped as such; everything else is a control frame. Sending
  a control frame as terminal bytes would type it into somebody's shell, and
  sending terminal bytes as a control frame would hand the far end something
  to act on.
* **Which name the bytes get back depends on who is listening.** A worker
  reads them as `input` — somebody typing at it — and a browser as `term` —
  the session printing. The same bytes, named for the direction they are
  travelling.
* **A bare JSON string is refused.** It is the one thing that looks like a
  control frame to a reader and is not one to the codec, so a caller that
  sends it silently loses the frame. The refusal is what makes the repository
  able to check that nothing does.

Everything is driven: the real codec and the real client, with the socket
replaced by a recorder.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_controlws_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.client import control_ws

OUT = Path(__file__).resolve().parent / "controlws_golden.json"

PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    ("somebody typing", {"type": "input", "data": "ls\n"}),
    ("the session printing", {"type": "term", "data": "hello\r\n"}),
    ("typing with nothing in it", {"type": "input"}),
    ("typing something that is not text", {"type": "input", "data": 42}),
    ("typing a delimiter", {"type": "input", "data": "\x10\x02{}"}),
    ("typing outside ASCII", {"type": "input", "data": "héllo ☃"}),
    ("a control frame", {"type": "hijack_request"}),
    ("a control frame with fields", {"type": "resize", "cols": 80, "rows": 25}),
    ("a frame with no type at all", {"data": "ls"}),
    ("a frame whose type is null", {"type": None, "data": "ls"}),
    ("a frame whose type is a number", {"type": 1, "data": "ls"}),
    ("a frame named input in capitals", {"type": "INPUT", "data": "ls"}),
    ("an empty frame", {}),
]


def _decoded(role: str, chunks: list[str]) -> list[dict[str, Any]]:
    """Feed a stream to the real decoder, one chunk at a time."""
    decoder = control_ws.LogicalFrameDecoder(role=role)  # type: ignore[arg-type]
    frames: list[dict[str, Any]] = []
    for chunk in chunks:
        frames.extend(decoder.feed(chunk))
    frames.extend(decoder.finish())
    return frames


STREAMS: list[tuple[str, list[str]]] = [
    ("terminal bytes", [control_ws.encode_logical_frame({"type": "term", "data": "hello"})]),
    ("a control frame", [control_ws.encode_logical_frame({"type": "hijack_request"})]),
    (
        "one of each",
        [
            control_ws.encode_logical_frame({"type": "term", "data": "before"})
            + control_ws.encode_logical_frame({"type": "hijack_state", "holder": "ada"})
            + control_ws.encode_logical_frame({"type": "term", "data": "after"})
        ],
    ),
    (
        "a frame split across two reads",
        [
            control_ws.encode_logical_frame({"type": "hello", "session_id": "s1"})[:6],
            control_ws.encode_logical_frame({"type": "hello", "session_id": "s1"})[6:],
        ],
    ),
    ("an escaped delimiter in the bytes", [control_ws.encode_logical_frame({"type": "term", "data": "\x10\x02{}"})]),
    ("nothing at all", [""]),
]


class RecordingSocket:
    """Stands in for the socket, writing down what the client handed it."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data.decode("latin-1") if isinstance(data, bytes) else data)

    async def recv(self) -> Any:
        raise AssertionError("not used")


async def _send_case(name: str, value: Any) -> dict[str, Any]:
    """Hand one value to the real client's `send` and record what happened."""
    socket = RecordingSocket()
    client = control_ws.AsyncInlineWebSocketClient(socket, role="browser")
    try:
        await client.send(value)
        outcome: dict[str, Any] = {"sent": socket.sent, "error": None}
    except Exception as exc:
        outcome = {"sent": socket.sent, "error": type(exc).__name__, "message": str(exc)}
    return {
        "name": name,
        "value": value.decode("latin-1") if isinstance(value, bytes) else value,
        "is_bytes": isinstance(value, bytes),
        **outcome,
    }


async def _send_json_case(name: str, value: Any) -> dict[str, Any]:
    """Hand one value to `send_json`, which takes only a mapping."""
    socket = RecordingSocket()
    client = control_ws.AsyncInlineWebSocketClient(socket, role="browser")
    try:
        await client.send_json(value)
        return {"name": name, "value": value, "sent": socket.sent, "error": None}
    except Exception as exc:
        return {"name": name, "value": value, "sent": socket.sent, "error": type(exc).__name__, "message": str(exc)}


async def _recv_cases() -> list[dict[str, Any]]:
    """What `recv_frame` hands back, one logical frame at a time."""

    class ScriptedSocket:
        def __init__(self, chunks: list[str]) -> None:
            self._chunks = list(chunks)

        async def recv(self) -> Any:
            if not self._chunks:
                raise AssertionError("read past the end of the script")
            return self._chunks.pop(0)

        async def send(self, data: Any) -> None:
            raise AssertionError("not used")

    cases: list[dict[str, Any]] = []
    for name, role, chunks, reads in (
        (
            "a browser reading the session",
            "browser",
            [
                control_ws.encode_logical_frame({"type": "term", "data": "out"})
                + control_ws.encode_logical_frame({"type": "hijack_state", "holder": "ada"})
            ],
            2,
        ),
        (
            "a worker reading what was typed",
            "worker",
            [control_ws.encode_logical_frame({"type": "input", "data": "ls"})],
            1,
        ),
        (
            "a frame that arrives in pieces",
            "browser",
            [
                control_ws.encode_logical_frame({"type": "hello", "session_id": "s1"})[:4],
                control_ws.encode_logical_frame({"type": "hello", "session_id": "s1"})[4:],
            ],
            1,
        ),
    ):
        socket = ScriptedSocket(chunks)
        client = control_ws.AsyncInlineWebSocketClient(socket, role=role)  # type: ignore[arg-type]
        frames = [await client.recv_frame() for _ in range(reads)]
        cases.append({"name": name, "role": role, "chunks": chunks, "frames": frames})

    # A payload that is not text at all.
    class BinarySocket:
        async def recv(self) -> Any:
            return b"\x01\x02"

        async def send(self, data: Any) -> None:
            raise AssertionError("not used")

    client = control_ws.AsyncInlineWebSocketClient(BinarySocket(), role="browser")
    try:
        await client.recv_frame()
        cases.append({"name": "a payload that is not text", "role": "browser", "chunks": [], "error": None})
    except Exception as exc:
        cases.append(
            {
                "name": "a payload that is not text",
                "role": "browser",
                "chunks": [],
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
    return cases


async def main_async() -> None:
    corpus = {
        "encoded": [
            {"name": name, "payload": payload, "encoded": control_ws.encode_logical_frame(payload)}
            for name, payload in PAYLOADS
        ],
        "decoded": [
            {"name": name, "role": role, "chunks": chunks, "frames": _decoded(role, chunks)}
            for name, chunks in STREAMS
            for role in ("browser", "worker")
        ],
        "sent": [
            await _send_case(*case)
            for case in (
                ("a mapping", {"type": "hijack_request"}),
                ("bytes", b"\x01\x02raw"),
                ("text that is not json", "just text"),
                ("a bare json object", '{"type":"hijack_request"}'),
                ("a bare json list", "[1,2]"),
                ("a bare json number", "42"),
                ("a bare json string", '"hello"'),
                ("a bare json null", "null"),
                ("something else entirely", 42),
            )
        ],
        "sent_json": [
            await _send_json_case(*case)
            for case in (
                ("a mapping", {"type": "hijack_request"}),
                ("a list", [1, 2]),
                ("text", "hello"),
                ("a number", 42),
                ("nothing", None),
            )
        ],
        "received": await _recv_cases(),
        "roles": {
            "browser_data_type": control_ws.LogicalFrameDecoder(role="browser")._data_type(),
            "worker_data_type": control_ws.LogicalFrameDecoder(role="worker")._data_type(),
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['encoded'])} payloads)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
