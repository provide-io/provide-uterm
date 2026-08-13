#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Wire-format golden-bytes snapshot tests for the frame builders.

These tests pin the exact dict shape every ``make_*_frame`` builder
emits, so a future change to ``schemas.py`` (or to the Pydantic
``model_dump`` defaults) that silently alters the wire format will fail
the suite. Together with the codegen drift check in
``scripts/codegen_frames.py``, this guards both Python ↔ TypeScript
schema parity and Python-side wire stability.

Each test fixes ``ts`` explicitly so the assertion is byte-for-byte.
"""

from __future__ import annotations

import json

import pytest

from provide.uterm.server.bridge.frames import (
    coerce_worker_status_frame,
    make_analysis_frame,
    make_error_frame,
    make_heartbeat_ack_frame,
    make_hello_frame,
    make_hijack_state_frame,
    make_pong_frame,
    make_snapshot_frame,
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)


class TestSimpleFrames:
    """Builders that emit a fixed key set with no optional null fields."""

    def test_error_frame_exact_shape(self) -> None:
        assert make_error_frame("boom") == {"type": "error", "message": "boom"}

    def test_pong_frame_exact_shape(self) -> None:
        assert make_pong_frame(ts=1.5) == {"type": "pong", "ts": 1.5}

    def test_heartbeat_ack_frame_exact_shape(self) -> None:
        assert make_heartbeat_ack_frame(2.5, ts=1.0) == {
            "type": "heartbeat_ack",
            "lease_expires_at": 2.5,
            "ts": 1.0,
        }

    def test_worker_connected_frame_exact_shape(self) -> None:
        assert make_worker_connected_frame("w1", ts=1.0) == {
            "type": "worker_connected",
            "worker_id": "w1",
            "ts": 1.0,
        }

    def test_worker_disconnected_frame_exact_shape(self) -> None:
        assert make_worker_disconnected_frame("w1", ts=1.0) == {
            "type": "worker_disconnected",
            "worker_id": "w1",
            "ts": 1.0,
        }

    def test_term_frame_exact_shape(self) -> None:
        assert make_term_frame("hi", ts=1.0) == {"type": "term", "data": "hi", "ts": 1.0}


class TestNullablePreservingFrames:
    """Builders whose schema must keep explicit ``null`` keys on the wire.

    The TypeScript consumer reads these fields directly off the message
    object; a missing key vs ``null`` is observably different on the
    consumer side. ``exclude_none=False`` in the builder is load-bearing.
    """

    def test_snapshot_frame_keeps_null_prompt_detected(self) -> None:
        out = make_snapshot_frame(
            screen="$ ",
            cursor={"row": 0, "col": 2},
            cols=80,
            rows=24,
            screen_hash="abc",
            cursor_at_end=True,
            has_trailing_space=True,
            prompt_detected=None,
            ts=1.0,
        )
        assert out == {
            "type": "snapshot",
            "screen": "$ ",
            "cursor": {"row": 0, "col": 2},
            "cols": 80,
            "rows": 24,
            "screen_hash": "abc",
            "cursor_at_end": True,
            "has_trailing_space": True,
            "prompt_detected": None,
            "raw_tail": None,
            # Ingest counters are absent here, and absent must stay explicitly
            # null rather than 0 — a producer that cannot report them has to
            # remain distinguishable from one that genuinely read nothing.
            "chunks_read": None,
            "bytes_read": None,
            "ts": 1.0,
        }
        # Belt-and-suspenders: round-trips through JSON without dropping the null.
        assert json.loads(json.dumps(out))["prompt_detected"] is None
        assert json.loads(json.dumps(out))["chunks_read"] is None

    def test_snapshot_frame_with_prompt_detected_payload(self) -> None:
        out = make_snapshot_frame(
            screen="$ ",
            cursor={"row": 0, "col": 2},
            cols=80,
            rows=24,
            screen_hash="abc",
            cursor_at_end=True,
            has_trailing_space=True,
            prompt_detected={"name": "bash", "confidence": 0.9},
            ts=1.0,
            event_seq=7,
        )
        assert out["prompt_detected"] == {"name": "bash", "confidence": 0.9}
        assert out["event_seq"] == 7

    def test_analysis_frame_keeps_null_raw(self) -> None:
        out = make_analysis_frame(formatted="ok", raw=None, ts=1.0)
        assert out == {"type": "analysis", "formatted": "ok", "raw": None, "ts": 1.0}
        assert json.loads(json.dumps(out))["raw"] is None

    def test_analysis_frame_with_raw_object(self) -> None:
        out = make_analysis_frame(formatted="ok", raw={"k": "v"}, ts=1.0)
        assert out == {"type": "analysis", "formatted": "ok", "raw": {"k": "v"}, "ts": 1.0}

    def test_hijack_state_frame_keeps_null_owner_and_lease(self) -> None:
        out = make_hijack_state_frame(
            hijacked=False,
            owner=None,
            lease_expires_at=None,
            input_mode="open",
        )
        assert out == {
            "type": "hijack_state",
            "hijacked": False,
            "owner": None,
            "lease_expires_at": None,
            "input_mode": "open",
        }
        parsed = json.loads(json.dumps(out))
        assert parsed["owner"] is None
        assert parsed["lease_expires_at"] is None

    def test_hijack_state_frame_with_owner_set(self) -> None:
        out = make_hijack_state_frame(
            hijacked=True,
            owner="dashboard",
            lease_expires_at=12.5,
            input_mode="hijack",
        )
        assert out == {
            "type": "hijack_state",
            "hijacked": True,
            "owner": "dashboard",
            "lease_expires_at": 12.5,
            "input_mode": "hijack",
        }


class TestLooseFrames:
    """Builders that pass arbitrary keys through without schema enforcement."""

    def test_hello_frame_passes_through_arbitrary_keys(self) -> None:
        out = make_hello_frame(
            worker_id="w1",
            can_hijack=True,
            future_capability_flag=True,
            capabilities={"hijack_step": True},
        )
        # Capability defaults from spec/behavior.json hello_defaults.python_fastapi.
        assert out == {
            "type": "hello",
            "worker_id": "w1",
            "can_hijack": True,
            "future_capability_flag": True,
            "capabilities": {"hijack_step": True},
            "mcp_supported": True,
            "vnc_supported": True,
        }

    def test_hello_frame_minimal(self) -> None:
        assert make_hello_frame() == {
            "type": "hello",
            "mcp_supported": True,
            "vnc_supported": True,
        }

    def test_coerce_worker_status_frame_stamps_type_and_ts(self) -> None:
        out = coerce_worker_status_frame({"foo": 1, "ts": 2.0})
        assert out == {"type": "status", "foo": 1, "ts": 2.0}

    def test_coerce_worker_status_frame_preserves_existing_type(self) -> None:
        out = coerce_worker_status_frame({"type": "status", "extra": "x", "ts": 3.0})
        assert out == {"type": "status", "extra": "x", "ts": 3.0}

    def test_coerce_worker_status_frame_supplies_default_ts(self) -> None:
        out = coerce_worker_status_frame({"foo": 1})
        # ts is default-filled; assert presence/shape, not the exact wall-clock value.
        assert out["type"] == "status"
        assert out["foo"] == 1
        assert isinstance(out["ts"], float)


class TestNumericTypePreservation:
    """The wire format distinguishes ``float`` from ``int`` for ``ts`` etc.

    A schema change that promotes ``ts: float`` → ``ts: int`` (or v.v.)
    breaks consumers that pattern-match on type. Pin both shapes.
    """

    @pytest.mark.parametrize("ts_value", [0.0, 1.5, 1_700_000_000.123])
    def test_term_frame_ts_stays_float(self, ts_value: float) -> None:
        out = make_term_frame("x", ts=ts_value)
        assert isinstance(out["ts"], float)
        assert out["ts"] == ts_value

    @pytest.mark.parametrize("lease", [0.0, 0.001, 3600.0])
    def test_heartbeat_ack_lease_stays_float(self, lease: float) -> None:
        out = make_heartbeat_ack_frame(lease, ts=1.0)
        assert isinstance(out["lease_expires_at"], float)
        assert out["lease_expires_at"] == lease
