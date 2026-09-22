#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate committed golden vectors from spec/behavior.json and the Python reference.

Emits a single JSON file consumed by Python, TypeScript, Go, and C# tests so
policy, hello defaults and client-side transport close semantics stay aligned
across the ports without placeholder asserts.

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
    }


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    vectors = build_vectors(spec)
    text = json.dumps(vectors, indent=2, sort_keys=True) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    for dest in (GO_COPY, GO_VNC_COPY, GO_TRANSPORTS_COPY, CS_COPY, PY_TEST_COPY, OUT_PATH):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(ROOT)} ({len(vectors['policy_cases'])} policy cases)")
    print(f"Copied to {GO_COPY.relative_to(ROOT)}")
    print(f"Copied to {GO_TRANSPORTS_COPY.relative_to(ROOT)}")
    print(f"Copied to {CS_COPY.relative_to(ROOT)}")
    print(f"Copied to {PY_TEST_COPY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
