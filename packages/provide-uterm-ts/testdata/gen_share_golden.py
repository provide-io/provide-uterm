#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for ``uterm share``.

Sharing a terminal hands somebody else a URL onto a live shell, so the
decisions before the first byte moves are the ones worth pinning:

* **Where the bearer token comes from**, in order: the flag, then the named
  file, then the default file, then nothing. A flag beating a file is what
  lets somebody override a stale credential without deleting it.
* **What the session is called**, since the display name is what a viewer
  sees on the other end.
* **Where the WebSocket actually is.** The server may answer with a full URL
  or a path, and a path is joined to the server it came from with the scheme
  upgraded — `http` to `ws`, `https` to `wss`. Getting that wrong is either a
  connection that fails or, worse, a shared terminal over cleartext.
* **What is printed**, because the share and control URLs are the whole
  output: one is for watching and one is for typing, and a caller who mixes
  them up hands over control.

The command is driven with the network, the PTY and the bridge replaced by
recorders, so what is recorded is what the real code decided.

The bridge itself is driven too: both directions, until either side closes —
and with it the frame protocol it writes, which is two bytes of header and
then the payload.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_share_golden.py
"""

# Every "token" below is a fixture the corpus feeds the reference, not a
# credential: the point of most of them is which one wins.
# ruff: noqa: S106

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.cli import share as cli_share

from provide.uterm.tunnel import protocol as tunnel_protocol

OUT = Path(__file__).resolve().parent / "share_golden.json"

TUNNEL_INFO: dict[str, Any] = {
    "tunnel_id": "t-1",
    "share_url": "https://warp.example/s/abc",
    "control_url": "https://warp.example/c/abc",
    "ws_endpoint": "/tunnel/abc",
    "worker_token": "worker-token",
}


def _protocol_cases() -> dict[str, Any]:
    """The wire frames, driven: two header bytes and then the payload."""

    def _refusal(call: Any) -> dict[str, Any]:
        try:
            value = call()
        except tunnel_protocol.TunnelProtocolError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"value": value.hex() if isinstance(value, bytes) else value}

    return {
        "channels": {
            "control": tunnel_protocol.CHANNEL_CONTROL,
            "data": tunnel_protocol.CHANNEL_DATA,
            "tcp": tunnel_protocol.CHANNEL_TCP,
            "http": tunnel_protocol.CHANNEL_HTTP,
        },
        "flags": {"data": tunnel_protocol.FLAG_DATA, "eof": tunnel_protocol.FLAG_EOF},
        "encoded": [
            {
                "name": name,
                "channel": channel,
                "payload": payload.decode("latin-1"),
                "flags": flags,
                **_refusal(
                    lambda channel=channel, payload=payload, flags=flags: tunnel_protocol.encode_frame(
                        channel, payload, flags=flags
                    )
                ),
            }
            for name, channel, payload, flags in (
                ("a data frame", tunnel_protocol.CHANNEL_DATA, b"hi", tunnel_protocol.FLAG_DATA),
                ("an empty payload", tunnel_protocol.CHANNEL_DATA, b"", tunnel_protocol.FLAG_DATA),
                ("an end-of-file frame", tunnel_protocol.CHANNEL_DATA, b"", tunnel_protocol.FLAG_EOF),
                ("the control channel", tunnel_protocol.CHANNEL_CONTROL, b"{}", tunnel_protocol.FLAG_DATA),
                ("the highest channel", 0xFF, b"x", 0xFF),
                ("bytes outside ASCII", tunnel_protocol.CHANNEL_DATA, "héllo ☃".encode(), 0),
                ("a channel past the end", 0x100, b"x", 0),
                ("a negative channel", -1, b"x", 0),
                ("flags past the end", tunnel_protocol.CHANNEL_DATA, b"x", 0x100),
                ("negative flags", tunnel_protocol.CHANNEL_DATA, b"x", -1),
            )
        ],
        "decoded": [
            {"name": name, "raw": raw.hex(), **_decode(raw)}
            for name, raw in (
                ("a data frame", b"\x01\x00hi"),
                ("an end-of-file frame", b"\x01\x01"),
                ("a control frame", b"\x00\x00{}"),
                ("a header and nothing else", b"\x01\x00"),
                ("one byte", b"\x01"),
                ("nothing at all", b""),
                ("every flag set", b"\x01\xffpayload"),
            )
        ],
        "control_encoded": [
            {
                "name": name,
                "message": message,
                **_refusal(lambda message=message: tunnel_protocol.encode_control(message)),
            }
            for name, message in (
                ("an ordinary message", {"type": "hello"}),
                ("a message with fields", {"type": "resize", "cols": 80, "rows": 25}),
                ("a message with no type", {"cols": 80}),
                ("an empty message", {}),
                ("a message whose type is null", {"type": None}),
                ("text outside ASCII", {"type": "héllo ☃"}),
            )
        ],
        "control_decoded": [
            {"name": name, "payload": payload.decode("latin-1"), **_decode_control(payload)}
            for name, payload in (
                ("an object", b'{"type":"hello"}'),
                ("an empty object", b"{}"),
                ("a list", b"[1,2]"),
                ("a string", b'"hello"'),
                ("a number", b"42"),
                ("null", b"null"),
                ("nothing at all", b""),
                ("broken json", b"{"),
                ("bytes that are not utf-8", b"\xff\xfe"),
                # Valid JSON structure with one byte that is not UTF-8: a
                # lenient decoder would turn it into a replacement character
                # and accept the message.
                ("json holding a byte that is not utf-8", b'{"type":"\xff"}'),
            )
        ],
    }


def _decode(raw: bytes) -> dict[str, Any]:
    """Decode a frame, recording the refusal if there is one."""
    try:
        frame = tunnel_protocol.decode_frame(raw)
    except tunnel_protocol.TunnelProtocolError as exc:
        return {"error": str(exc)}
    return {
        "channel": frame.channel,
        "flags": frame.flags,
        "payload": frame.payload.decode("latin-1"),
        "is_eof": frame.is_eof,
        "is_control": frame.is_control,
    }


def _decode_control(payload: bytes) -> dict[str, Any]:
    """Decode a control payload, recording the refusal if there is one."""
    try:
        return {"value": tunnel_protocol.decode_control(payload)}
    except tunnel_protocol.TunnelProtocolError as exc:
        return {"error": str(exc)}


def _namespace(**fields: Any) -> argparse.Namespace:
    """The parsed arguments as the CLI builds them."""
    defaults: dict[str, Any] = {"server": "https://warp.example", "cmd": None, "attach": False}
    defaults.update(fields)
    return argparse.Namespace(**defaults)


def _token_cases() -> list[dict[str, Any]]:
    """Where the bearer token is taken from, driven against real files."""
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as directory:
        named = Path(directory) / "named-token"
        named.write_text("  from-named-file  \n")
        empty = Path(directory) / "empty-token"
        empty.write_text("   \n")
        missing = Path(directory) / "nothing-here"

        # The temporary directory is different on every run, so each path is
        # recorded under a stable label instead — a corpus naming a path that
        # changes could never be checked for drift.
        labels: dict[str, str] = {
            str(named): "a file holding a token",
            str(empty): "a file holding only spaces",
            str(missing): "a path with no file at it",
            directory: "a path that is a directory",
        }
        contents = {
            "a file holding a token": named.read_text(),
            "a file holding only spaces": empty.read_text(),
        }

        for name, args in (
            ("a token on the command line", _namespace(token="from-flag", token_file=str(named))),
            ("a named token file", _namespace(token=None, token_file=str(named))),
            ("a named file that is not there", _namespace(token=None, token_file=str(missing))),
            ("a named file holding only spaces", _namespace(token=None, token_file=str(empty))),
            ("a token file that is a directory", _namespace(token=None, token_file=directory)),
            ("an empty token on the command line", _namespace(token="", token_file=str(named))),
            ("no token argument at all", _namespace()),
        ):
            given = getattr(args, "token_file", None)
            cases.append(
                {
                    "name": name,
                    "token": getattr(args, "token", None),
                    "token_file": None if given is None else labels[given],
                    "file_contents": contents,
                    "resolved": cli_share._read_token(args),
                }
            )
    return cases


def _display_cases() -> list[dict[str, Any]]:
    """What the session is called, with the environment replaced."""
    cases: list[dict[str, Any]] = []
    real_getuser = cli_share.getpass.getuser
    real_node = cli_share.platform.node
    try:
        for name, given, user, node in (
            ("a name given on the command line", "my session", "ada", "workstation"),
            ("no name given", None, "ada", "workstation"),
            ("a user nobody can name", None, RuntimeError("no such user"), "workstation"),
            ("a host with no name", None, "ada", ""),
            ("neither a user nor a host", None, RuntimeError("no such user"), ""),
        ):

            def _getuser(value: Any = user) -> str:
                if isinstance(value, Exception):
                    raise value
                return str(value)

            cli_share.getpass.getuser = _getuser  # type: ignore[assignment]
            cli_share.platform.node = lambda value=node: str(value)  # type: ignore[assignment]
            cases.append(
                {
                    "name": name,
                    "display_name": given,
                    "user": "raises" if isinstance(user, Exception) else user,
                    "node": node,
                    "resolved": cli_share._display_name(_namespace(display_name=given)),
                }
            )
    finally:
        cli_share.getpass.getuser = real_getuser  # type: ignore[assignment]
        cli_share.platform.node = real_node  # type: ignore[assignment]
    return cases


class RecordingPty:
    """Stands in for the PTY the command would otherwise spawn."""

    def __init__(self, log: list[Any], kind: str) -> None:
        self._log = log
        self._kind = kind

    def start(self) -> None:
        self._log.append(["start", self._kind])

    def close(self) -> None:
        self._log.append(["close", self._kind])


def _run_command(name: str, args: argparse.Namespace, tunnel_info: dict[str, Any]) -> dict[str, Any]:
    """Drive the real command with the network, the PTY and the bridge replaced."""
    log: list[Any] = []
    ran: dict[str, Any] = {}

    def fake_create(server: str, display_name: str, token: str | None) -> dict[str, Any]:
        log.append(["create", server, display_name, token])
        return dict(tunnel_info)

    async def fake_run_share(pty_source: Any, ws_endpoint: str, worker_token: str, *, attach: bool) -> None:
        ran.update({"ws_endpoint": ws_endpoint, "worker_token": worker_token, "attach": attach})
        log.append(["bridge", ws_endpoint])

    real_create = cli_share._create_tunnel
    real_run = cli_share._run_share
    real_spawn = cli_share.spawn_pty
    real_proxy = cli_share.TtyProxy
    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code: int | None = None
    try:
        cli_share._create_tunnel = fake_create  # type: ignore[assignment]
        cli_share._run_share = fake_run_share  # type: ignore[assignment]
        cli_share.spawn_pty = lambda cmd: RecordingPty(log, f"spawned:{cmd}")  # type: ignore[assignment]
        cli_share.TtyProxy = lambda: RecordingPty(log, "attached")  # type: ignore[assignment]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli_share._cmd_share(args)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
    finally:
        cli_share._create_tunnel = real_create  # type: ignore[assignment]
        cli_share._run_share = real_run  # type: ignore[assignment]
        cli_share.spawn_pty = real_spawn  # type: ignore[assignment]
        cli_share.TtyProxy = real_proxy  # type: ignore[assignment]

    return {
        "name": name,
        "server": args.server,
        "attach": getattr(args, "attach", False),
        "cmd": args.cmd,
        "tunnel_info": tunnel_info,
        "log": log,
        "ran": ran,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "exit_code": exit_code,
    }


def _command_cases() -> list[dict[str, Any]]:
    """The whole command, over the shapes a server's answer can take."""
    cases: list[tuple[str, argparse.Namespace, dict[str, Any]]] = [
        ("a relative endpoint over https", _namespace(token="t", display_name="d"), TUNNEL_INFO),
        (
            "a relative endpoint over http",
            _namespace(server="http://warp.example", token="t", display_name="d"),
            TUNNEL_INFO,
        ),
        (
            "a server named with a trailing slash",
            _namespace(server="https://warp.example/", token="t", display_name="d"),
            TUNNEL_INFO,
        ),
        (
            "an absolute endpoint",
            _namespace(token="t", display_name="d"),
            {**TUNNEL_INFO, "ws_endpoint": "wss://elsewhere.example/tunnel/abc"},
        ),
        (
            "an absolute endpoint over cleartext",
            _namespace(token="t", display_name="d"),
            {**TUNNEL_INFO, "ws_endpoint": "ws://elsewhere.example/tunnel/abc"},
        ),
        ("an answer with no endpoint", _namespace(token="t", display_name="d"), {**TUNNEL_INFO, "ws_endpoint": ""}),
        ("an answer with nothing in it", _namespace(token="t", display_name="d"), {}),
        (
            "an answer with no urls to print",
            _namespace(token="t", display_name="d"),
            {"ws_endpoint": "/tunnel/abc", "worker_token": "w"},
        ),
        ("attaching to this terminal", _namespace(token="t", display_name="d", attach=True), TUNNEL_INFO),
        ("sharing a command", _namespace(token="t", display_name="d", cmd=["htop"]), TUNNEL_INFO),
        (
            "a server whose path mentions http",
            _namespace(server="https://warp.example/http://x", token="t", display_name="d"),
            TUNNEL_INFO,
        ),
    ]
    return [_run_command(name, args, info) for name, args, info in cases]


class BridgeSource:
    """A PTY that hands over a script of reads and records what it is given."""

    def __init__(self, log: list[Any], reads: list[str | None]) -> None:
        self._log = log
        self._reads = list(reads)

    async def read(self, size: int) -> bytes:
        if not self._reads:
            raise EOFError("nothing left")
        value = self._reads.pop(0)
        self._log.append(["read", size, value])
        if value is None:
            raise OSError("the pty went away")
        return value.encode()

    async def write(self, data: bytes) -> None:
        self._log.append(["write", data.decode()])

    async def write_local(self, data: bytes) -> None:
        self._log.append(["write_local", data.decode()])


async def _drive_bridge(
    name: str, reads: list[str | None], receives: list[str | None], is_attach: bool
) -> dict[str, Any]:
    """Run the real bridge over scripted sides, recording every crossing."""
    log: list[Any] = []
    source = BridgeSource(log, reads)
    pending = list(receives)

    async def ws_send(frame: bytes) -> None:
        log.append(["send", frame.hex()])

    async def ws_recv() -> bytes:
        if not pending:
            raise EOFError("nothing left")
        value = pending.pop(0)
        log.append(["recv", value])
        if value is None:
            raise OSError("the socket went away")
        return value.encode()

    await cli_share._bridge_loop(source, ws_send, ws_recv, is_attach=is_attach)
    return {"name": name, "reads": reads, "receives": receives, "is_attach": is_attach, "log": log}


BRIDGES: list[tuple[str, list[str | None], list[str | None], bool]] = [
    ("both sides quiet", [""], [""], False),
    ("a byte each way", ["hi", ""], ["there", ""], False),
    ("the pty closing first", [""], ["a", "b", ""], False),
    ("the socket closing first", ["a", "b", ""], [""], False),
    ("the pty failing", [None], [""], False),
    ("the socket failing", [""], [None], False),
    ("attached, so the terminal is written to", [""], ["there", ""], True),
    ("not attached, so the pty is written to", [""], ["there", ""], False),
    ("text outside ASCII", ["héllo ☃", ""], ["☃", ""], False),
]


async def _bridges() -> list[dict[str, Any]]:
    """Drive every bridge case."""
    return [await _drive_bridge(*case) for case in BRIDGES]


def main() -> None:
    # The command cases run outside a loop: `_cmd_share` calls `asyncio.run`
    # itself, which cannot be done from inside one.
    corpus = {
        "protocol": _protocol_cases(),
        "tokens": _token_cases(),
        "display_names": _display_cases(),
        "commands": _command_cases(),
        "bridges": asyncio.run(_bridges()),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['commands'])} commands)")


if __name__ == "__main__":
    main()
