#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate committed golden vectors from spec/behavior.json and the Python reference.

Emits a single JSON file consumed by Python, TypeScript, Go, and C# tests so
policy, hello defaults and client-side transport close semantics stay aligned
across the ports without placeholder asserts.

``capture_geometry_frame`` is the capture wire's geometry channel: whole frames
(header included) and the size the Python reference
(``provide.uterm.pty.capture.parse_geometry``) reads from each, or null where it
ignores one. The Python and Go capture connectors and the TypeScript capture
reader replay them.

``close_cases`` is computed from the shipped Python reference
(``provide.uterm.transport_close`` and the WebSocket attribution in
``provide.uterm.transports.ws_transport``) where a pure function exists. The
telnet and chaos expectations need a live transport to observe, so they are
declared here and the Python suites drive the real transports against every
one of them (``test_transport_close_mapping.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "spec" / "behavior.json"
OUT_PATH = ROOT / "spec" / "behavior_vectors.json"
GO_COPY = ROOT / "packages" / "provide-uterm-go" / "policy" / "testdata" / "behavior_vectors.json"
GO_VNC_COPY = ROOT / "packages" / "provide-uterm-go" / "vnc" / "testdata" / "behavior_vectors.json"
GO_TRANSPORTS_COPY = ROOT / "packages" / "provide-uterm-go" / "transports" / "testdata" / "behavior_vectors.json"
CS_COPY = (
    ROOT
    / "packages"
    / "provide-uterm-csharp"
    / "tests"
    / "Provide.Uterm.Tests"
    / "testdata"
    / "behavior"
    / "behavior_vectors.json"
)
PY_TEST_COPY = ROOT / "packages" / "provide-uterm" / "tests" / "bridge" / "testdata" / "behavior_vectors.json"
GO_PTY_COPY = ROOT / "packages" / "provide-uterm-go" / "pty" / "testdata" / "behavior_vectors.json"
PLATFORM_TEST_COPY = (
    ROOT / "packages" / "provide-uterm-platform" / "tests" / "pty" / "testdata" / "behavior_vectors.json"
)
# Every committed copy beside the tests that replay it; scripts/check_protocol_drift.py
# holds them byte-identical to OUT_PATH. TypeScript reads spec/ directly.
COPIES = (GO_COPY, GO_VNC_COPY, GO_TRANSPORTS_COPY, CS_COPY, PY_TEST_COPY, GO_PTY_COPY, PLATFORM_TEST_COPY)


def _role_ok(role: str, minimum: str, roles: dict[str, dict[str, int]]) -> bool:
    return roles[role]["rank"] >= roles[minimum]["rank"]


# summary(): every combination of code/reason/detail present or absent, for
# every initiator, plus a zero code (a code, not an absent one).
_SUMMARY_CODE = 1001
_SUMMARY_REASON = "going away"
_SUMMARY_DETAIL = "ConnectionResetError: reset"

# WebSocket attribution inputs: (name, received, sent, received_then_sent).
# websockets only orders two frames, so received_then_sent is null unless both
# were exchanged.
_WS_FRAME_CASES: tuple[tuple[str, tuple[int, str] | None, tuple[int, str] | None, bool | None], ...] = (
    ("sent-only-keepalive-timeout", None, (1011, "keepalive ping timeout"), None),
    ("received-only", (1001, "going away"), None, None),
    ("received-then-sent", (1000, ""), (1000, ""), True),
    ("sent-then-received", (1000, ""), (1000, "bye"), False),
    ("neither", None, None, None),
)

# Telnet: the socket event, the operation it happened on, and the close the
# transport must report. A peer reset is the remote end closing; a broken pipe
# cannot say who did; the receive-buffer cap is this end giving up.
_TELNET_CASES: tuple[dict[str, str | None], ...] = (
    {"name": "eof", "event": "eof", "operation": "receive", "initiator": "remote", "code": None, "reason": ""},
    {
        "name": "reset-on-receive",
        "event": "reset",
        "operation": "receive",
        "initiator": "remote",
        "code": None,
        "reason": "",
    },
    {"name": "reset-on-send", "event": "reset", "operation": "send", "initiator": "remote", "code": None, "reason": ""},
    {
        "name": "broken-pipe-on-receive",
        "event": "broken_pipe",
        "operation": "receive",
        "initiator": "unknown",
        "code": None,
        "reason": "",
    },
    {
        "name": "broken-pipe-on-send",
        "event": "broken_pipe",
        "operation": "send",
        "initiator": "unknown",
        "code": None,
        "reason": "",
    },
    {
        "name": "rx-buffer-cap",
        "event": "rx_buffer_cap",
        "operation": "receive",
        "initiator": "local",
        "code": None,
        "reason": "receive buffer exceeded",
    },
)

_CHAOS_CASES: tuple[dict[str, str | None], ...] = (
    {
        "name": "injected-disconnect",
        "event": "injected_disconnect",
        "operation": "receive",
        "initiator": "unknown",
        "code": None,
        "reason": "injected disconnect",
    },
)


def _close_summary_cases() -> list[dict]:
    from provide.uterm.transport_close import CloseInitiator, TransportClose

    cases: list[dict] = []
    for initiator in CloseInitiator:
        for code in (_SUMMARY_CODE, None):
            for reason in (_SUMMARY_REASON, ""):
                for detail in (_SUMMARY_DETAIL, ""):
                    close = TransportClose(initiator, code=code, reason=reason, detail=detail)
                    cases.append(
                        {
                            "initiator": initiator.value,
                            "code": code,
                            "reason": reason,
                            "detail": detail,
                            "summary": close.summary(),
                        }
                    )
    zero = TransportClose(CloseInitiator.REMOTE, code=0)
    cases.append({"initiator": "remote", "code": 0, "reason": "", "detail": "", "summary": zero.summary()})
    return cases


def _close_websocket_cases() -> list[dict]:
    from provide.uterm.transports.ws_transport import _close_from_websockets
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close

    def frame(value: tuple[int, str] | None) -> Close | None:
        return None if value is None else Close(*value)

    def as_json(value: tuple[int, str] | None) -> dict | None:
        return None if value is None else {"code": value[0], "reason": value[1]}

    cases: list[dict] = []
    for name, received, sent, received_then_sent in _WS_FRAME_CASES:
        close = _close_from_websockets(ConnectionClosed(frame(received), frame(sent), received_then_sent))
        cases.append(
            {
                "name": name,
                "received": as_json(received),
                "sent": as_json(sent),
                "received_then_sent": received_then_sent,
                "initiator": close.initiator.value,
                "code": close.code,
                "reason": close.reason,
                "detail": close.detail,
            }
        )
    return cases


# (name, payload) for the geometry channel: the shape the brief fixes (>HH,
# cols then rows), both inclusive bounds, one past each, and the wrong lengths.
_GEOMETRY_PAYLOADS: tuple[tuple[str, bytes], ...] = (
    ("100x40", bytes.fromhex("00640028")),
    ("1x1", bytes.fromhex("00010001")),
    ("1000x1000", bytes.fromhex("03e803e8")),
    ("0-cols", bytes.fromhex("00000028")),
    ("0-rows", bytes.fromhex("00640000")),
    ("1001-cols", bytes.fromhex("03e90028")),
    ("1001-rows", bytes.fromhex("006403e9")),
    ("65535x65535", bytes.fromhex("ffffffff")),
    ("empty-payload", b""),
    ("three-byte-payload", bytes.fromhex("006400")),
    ("five-byte-payload", bytes.fromhex("0064002800")),
)


def build_capture_geometry_cases() -> list[dict]:
    """Geometry frames on the capture wire, and the size the reference reads from each."""
    import struct

    from provide.uterm.pty.capture import CHANNEL_GEOMETRY, parse_geometry

    cases: list[dict] = []
    for name, payload in _GEOMETRY_PAYLOADS:
        size = parse_geometry(payload)
        cases.append(
            {
                "name": name,
                "frame": (struct.pack(">BI", CHANNEL_GEOMETRY, len(payload)) + payload).hex(),
                "size": None if size is None else {"cols": size[0], "rows": size[1]},
            }
        )
    return cases


# (name, payload) for the input channel: a UTF-8 absolute path (the keystroke
# socket's path), an empty payload (withdraw), and the malformed shapes a
# producer might send. The convention check (.sock -> .stdin.sock) is the
# connector's job, not the parser's; these vectors test the parser only.
_INPUT_PAYLOADS: tuple[tuple[str, bytes], ...] = (
    ("absolute-path", b"/run/uterm-cap-12345.stdin.sock"),
    ("root-only", b"/"),
    ("withdraw-empty", b""),
    ("relative-path", b"relative/path.sock"),
    ("non-utf8", b"/run/\xff\xfe.sock"),
    ("over-max-len", b"/" + b"a" * 4096),  # 4097 bytes, one past INPUT_MAX_LEN
)


def build_capture_input_cases() -> list[dict]:
    """Input frames on the capture wire, and the path the reference reads from each."""
    import struct

    from provide.uterm.pty.capture import CHANNEL_INPUT, parse_input_path

    cases: list[dict] = []
    for name, payload in _INPUT_PAYLOADS:
        path = parse_input_path(payload)
        cases.append(
            {
                "name": name,
                "frame": (struct.pack(">BI", CHANNEL_INPUT, len(payload)) + payload).hex(),
                "path": path,
            }
        )
    return cases


def build_close_cases() -> dict:
    """Client-side transport close semantics every port is tested against."""
    return {
        "summary": _close_summary_cases(),
        "websocket": _close_websocket_cases(),
        "telnet": [dict(case) for case in _TELNET_CASES],
        "chaos": [dict(case) for case in _CHAOS_CASES],
    }


def build_vectors(spec: dict) -> dict:
    roles = spec["roles"]
    ops = spec["operations"]
    cases: list[dict] = []

    role_names = list(roles.keys())
    for op_name, op in ops.items():
        min_role = op["minimum_role"]
        preconditions = set(op.get("preconditions") or [])
        errors = op.get("error_codes") or {}
        for role in role_names:
            for lease_owned in (True, False):
                for session_active in (True, False):
                    allowed = True
                    error: str | None = None
                    if not _role_ok(role, min_role, roles):
                        allowed = False
                        error = errors.get("forbidden_role") or errors.get("403") or "forbidden: insufficient role"
                    elif "lease_owned" in preconditions and not lease_owned:
                        allowed = False
                        error = errors.get("forbidden_lease") or "forbidden: no active lease"
                    elif "session_active" in preconditions and not session_active:
                        allowed = False
                        error = errors.get("forbidden_session") or "forbidden: session inactive"
                    # Idempotent release with empty preconditions always allows once role ok.
                    cases.append(
                        {
                            "op": op_name,
                            "role": role,
                            "lease_owned": lease_owned,
                            "session_active": session_active,
                            "allowed": allowed,
                            "error": error,
                        }
                    )

    return {
        "version": spec["version"],
        "hello_defaults": spec["hello_defaults"],
        "policy_cases": cases,
        "close_cases": build_close_cases(),
        "capture_geometry_frame": build_capture_geometry_cases(),
        "capture_input_frame": build_capture_input_cases(),
    }


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    vectors = build_vectors(spec)
    text = json.dumps(vectors, indent=2, sort_keys=True) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    for dest in COPIES:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(ROOT)} ({len(vectors['policy_cases'])} policy cases)")
    for dest in COPIES:
        print(f"Copied to {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
