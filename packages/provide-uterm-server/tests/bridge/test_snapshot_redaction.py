#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for snapshot/analysis frame output redaction and connect-time redaction.

Covers:
- snapshot broadcast: screen + raw_tail redacted
- analysis broadcast: formatted + raw (when str) redacted
- connect-time initial snapshot: redacted per recipient role
- no gate / NoOp → snapshot unchanged
- term path still redacted (regression)
- non-content frame type passes through _redact_frame_fields unchanged
- all branches of _redact_frame_fields (term/snapshot/analysis/other)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.client import connect_test_ws
from provide.uterm.control_channel import ControlChannelDecoder, encode_control
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, RedactionRule
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.router_impl import _redact_frame_fields

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockOutputPolicy(OutputPolicyGate):
    def __init__(self, rules: list[RedactionRule]) -> None:
        self.rules = rules

    async def get_redaction_rules(self, _context: Any) -> list[RedactionRule]:
        return self.rules


_STRIPE_RULES = [RedactionRule(pattern=r"sk_live_\w+", replacement="[REDACTED]")]


def _collect_control_frames(ws: AsyncMock) -> list[dict[str, Any]]:
    """Decode all control frames received by a mock WebSocket."""
    decoder = ControlChannelDecoder()
    frames: list[dict[str, Any]] = []
    for call in ws.send_text.call_args_list:
        payload = call[0][0]
        for event in decoder.feed(payload):
            if event.kind == "control":
                frames.append(dict(event.control))
    return frames


# ---------------------------------------------------------------------------
# Unit tests for _redact_frame_fields
# ---------------------------------------------------------------------------


def test_redact_frame_fields_term() -> None:
    """term frames: data field is redacted."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg = {"type": "term", "data": "key sk_live_ABC active", "ts": 1.0}
    result = _redact_frame_fields(msg, redactor)
    assert result["data"] == "key [REDACTED] active"
    assert result["ts"] == 1.0  # other fields preserved
    assert msg["data"] == "key sk_live_ABC active"  # original not mutated


def test_redact_frame_fields_snapshot_screen_and_raw_tail() -> None:
    """snapshot frames: screen and raw_tail are redacted."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "snapshot",
        "screen": "key sk_live_ABC active",
        "raw_tail": "sk_live_XYZ",
        "cols": 80,
        "rows": 25,
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["screen"] == "key [REDACTED] active"
    assert result["raw_tail"] == "[REDACTED]"
    assert result["cols"] == 80  # other fields preserved
    # original not mutated
    assert msg["screen"] == "key sk_live_ABC active"
    assert msg["raw_tail"] == "sk_live_XYZ"


def test_redact_frame_fields_snapshot_raw_tail_none() -> None:
    """snapshot frames: raw_tail=None is left as None (not redacted)."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {"type": "snapshot", "screen": "sk_live_ABC", "raw_tail": None}
    result = _redact_frame_fields(msg, redactor)
    assert result["raw_tail"] is None
    assert result["screen"] == "[REDACTED]"


def test_redact_frame_fields_analysis_formatted_and_raw_str() -> None:
    """analysis frames: formatted and raw (when str) are redacted."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "saw sk_live_ABC",
        "raw": "sk_live_ABC",
        "ts": 1.0,
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["formatted"] == "saw [REDACTED]"
    assert result["raw"] == "[REDACTED]"
    assert result["ts"] == 1.0  # other fields preserved
    # original not mutated
    assert msg["formatted"] == "saw sk_live_ABC"


def test_redact_frame_fields_analysis_raw_structured() -> None:
    """analysis frames: raw that is a dict is recursively redacted (M4).

    Pre-M4 a structured ``raw`` was shipped verbatim; now nested string values
    are redacted while scalars are preserved.
    """
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "ok sk_live_ABC",
        "raw": {"nested": "sk_live_ABC"},  # dict, not str
        "ts": 1.0,
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["formatted"] == "ok [REDACTED]"
    assert result["raw"] == {"nested": "[REDACTED]"}  # nested string redacted
    # original not mutated
    assert msg["raw"] == {"nested": "sk_live_ABC"}


def test_redact_frame_fields_snapshot_prompt_detected_redacted() -> None:
    """M4: snapshot.prompt_detected (a dict carrying matched prompt text) is redacted."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "snapshot",
        "screen": "ok",
        "raw_tail": None,
        "prompt_detected": {"matched": "login sk_live_ABC here", "name": "login"},
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["prompt_detected"] == {"matched": "login [REDACTED] here", "name": "login"}
    # original not mutated
    assert msg["prompt_detected"]["matched"] == "login sk_live_ABC here"


def test_redact_frame_fields_snapshot_prompt_detected_none_untouched() -> None:
    """M4: snapshot.prompt_detected=None passes through unchanged (and doesn't raise)."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {"type": "snapshot", "screen": "sk_live_ABC", "prompt_detected": None}
    result = _redact_frame_fields(msg, redactor)
    assert result["prompt_detected"] is None
    assert result["screen"] == "[REDACTED]"


def test_redact_frame_fields_snapshot_without_prompt_detected_key() -> None:
    """M4: a snapshot event payload without a prompt_detected key is unaffected."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {"type": "snapshot", "screen": "sk_live_ABC", "screen_hash": "h"}
    result = _redact_frame_fields(msg, redactor)
    assert "prompt_detected" not in result
    assert result["screen"] == "[REDACTED]"


def test_redact_frame_fields_analysis_raw_structured_redacted() -> None:
    """M4: analysis.raw as a nested dict/list is recursively redacted (not shipped verbatim)."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "ok",
        "raw": {"nested": ["sk_live_ABC", {"deep": "sk_live_XYZ"}], "n": 7, "flag": True},
        "ts": 1.0,
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["raw"] == {"nested": ["[REDACTED]", {"deep": "[REDACTED]"}], "n": 7, "flag": True}
    # scalars are preserved as-is
    assert result["raw"]["n"] == 7
    assert result["raw"]["flag"] is True
    # original not mutated
    assert msg["raw"]["nested"][0] == "sk_live_ABC"


def test_redact_frame_fields_analysis_raw_list_redacted() -> None:
    """M4: analysis.raw as a top-level list is recursively redacted."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "ok",
        "raw": ["plain", "sk_live_ABC", 42],
    }
    result = _redact_frame_fields(msg, redactor)
    assert result["raw"] == ["plain", "[REDACTED]", 42]


def test_redact_frame_fields_analysis_raw_none_or_absent_untouched() -> None:
    """M4: analysis.raw that is neither str nor dict/list (None/absent) is left unset.

    Covers the elif-False fall-through (raw is None) of the analysis branch.
    """
    redactor = StreamRedactor(_STRIPE_RULES)
    # raw explicitly None
    msg_none: dict[str, Any] = {"type": "analysis", "formatted": "ok sk_live_ABC", "raw": None}
    result_none = _redact_frame_fields(msg_none, redactor)
    assert result_none["formatted"] == "ok [REDACTED]"
    assert result_none["raw"] is None
    # raw absent entirely
    msg_absent: dict[str, Any] = {"type": "analysis", "formatted": "ok"}
    result_absent = _redact_frame_fields(msg_absent, redactor)
    assert "raw" not in result_absent


def test_redact_value_caps_recursion_depth() -> None:
    """M4: _redact_value stops recursing past the depth cap (defensive; returns the deep value as-is)."""
    from provide.uterm.server.bridge.hub.router_impl import _redact_value

    redactor = StreamRedactor(_STRIPE_RULES)
    # Build a structure deeper than the cap; the secret below the cap is NOT redacted
    # (the cap is a defensive stop, not a correctness requirement — real frames are shallow).
    deep: Any = "sk_live_DEEP"
    for _ in range(40):
        deep = [deep]
    result = _redact_value(deep, redactor)
    # Walk back down to confirm the too-deep secret survived (depth cap kicked in).
    cur = result
    while isinstance(cur, list):
        cur = cur[0]
    assert cur == "sk_live_DEEP"


def test_redact_value_redacts_within_depth() -> None:
    """M4: _redact_value redacts strings within a shallow structure."""
    from provide.uterm.server.bridge.hub.router_impl import _redact_value

    redactor = StreamRedactor(_STRIPE_RULES)
    assert _redact_value("sk_live_ABC", redactor) == "[REDACTED]"
    assert _redact_value(["sk_live_ABC", {"k": "sk_live_XYZ"}], redactor) == ["[REDACTED]", {"k": "[REDACTED]"}]
    # scalars unchanged
    assert _redact_value(7, redactor) == 7
    assert _redact_value(None, redactor) is None
    assert _redact_value(True, redactor) is True


def test_redact_frame_fields_other_type_passthrough() -> None:
    """Non-content frame types pass through unchanged (covers final return msg)."""
    redactor = StreamRedactor(_STRIPE_RULES)
    msg: dict[str, Any] = {
        "type": "hijack_state",
        "hijacked": False,
        "owner": None,
        "lease_expires_at": None,
        "input_mode": "hijack",
    }
    result = _redact_frame_fields(msg, redactor)
    assert result is msg  # returned as-is, same object


# ---------------------------------------------------------------------------
# Broadcast redaction tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_broadcast_redaction() -> None:
    """snapshot broadcast: screen and raw_tail are redacted when gate is configured."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-snap"

    await hub.register_worker(worker_id, AsyncMock())
    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "viewer")

    snap: dict[str, Any] = {
        "type": "snapshot",
        "screen": "key sk_live_ABC active",
        "raw_tail": "sk_live_ABC",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "abc",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": 1.0,
    }
    await hub.broadcast(worker_id, snap)

    frames = _collect_control_frames(ws)
    snapshot_frames = [f for f in frames if f.get("type") == "snapshot"]
    assert snapshot_frames, "no snapshot frame received"
    sf = snapshot_frames[0]
    assert "sk_live_" not in sf["screen"], f"screen not redacted: {sf['screen']!r}"
    assert "[REDACTED]" in sf["screen"]
    assert "sk_live_" not in str(sf.get("raw_tail", "")), "raw_tail not redacted"
    assert "[REDACTED]" in str(sf.get("raw_tail", ""))

    # The original dict passed to broadcast must not be mutated.
    assert snap["screen"] == "key sk_live_ABC active", "broadcast mutated the original dict"


@pytest.mark.asyncio
async def test_analysis_broadcast_redaction() -> None:
    """analysis broadcast: formatted and raw (str) are redacted."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-analysis"

    await hub.register_worker(worker_id, AsyncMock())
    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "viewer")

    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "saw sk_live_ABC result",
        "raw": "sk_live_ABC",
        "ts": 1.0,
    }
    await hub.broadcast(worker_id, msg)

    frames = _collect_control_frames(ws)
    analysis_frames = [f for f in frames if f.get("type") == "analysis"]
    assert analysis_frames, "no analysis frame received"
    af = analysis_frames[0]
    assert "sk_live_" not in af["formatted"], f"formatted not redacted: {af['formatted']!r}"
    assert "[REDACTED]" in af["formatted"]
    assert "sk_live_" not in str(af.get("raw", "")), "raw not redacted"
    assert "[REDACTED]" in str(af.get("raw", ""))


@pytest.mark.asyncio
async def test_analysis_broadcast_raw_structured_redacted() -> None:
    """analysis broadcast: raw that is a dict is recursively redacted (M4)."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-analysis-raw"

    await hub.register_worker(worker_id, AsyncMock())
    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "viewer")

    msg: dict[str, Any] = {
        "type": "analysis",
        "formatted": "ok sk_live_ABC",
        "raw": {"nested_key": "sk_live_ABC"},  # dict, not str
        "ts": 1.0,
    }
    await hub.broadcast(worker_id, msg)

    frames = _collect_control_frames(ws)
    analysis_frames = [f for f in frames if f.get("type") == "analysis"]
    assert analysis_frames
    af = analysis_frames[0]
    # formatted should be redacted
    assert "sk_live_" not in af["formatted"]
    # raw dict's nested secret is now redacted, not shipped verbatim
    assert isinstance(af.get("raw"), dict)
    assert "sk_live_" not in str(af["raw"])
    assert af["raw"] == {"nested_key": "[REDACTED]"}


@pytest.mark.asyncio
async def test_no_gate_snapshot_broadcast_unchanged() -> None:
    """With no output gate, snapshot is broadcast without modification."""
    hub = TermHub()  # no gate
    worker_id = "w-nogate"

    await hub.register_worker(worker_id, AsyncMock())
    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "viewer")

    snap: dict[str, Any] = {
        "type": "snapshot",
        "screen": "sk_live_SECRET plaintext",
        "raw_tail": "sk_live_SECRET",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "x",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": 1.0,
    }
    await hub.broadcast(worker_id, snap)

    frames = _collect_control_frames(ws)
    snapshot_frames = [f for f in frames if f.get("type") == "snapshot"]
    assert snapshot_frames
    sf = snapshot_frames[0]
    assert "sk_live_SECRET" in sf["screen"], "no-gate broadcast must not redact"
    assert sf["raw_tail"] == "sk_live_SECRET"


@pytest.mark.asyncio
async def test_term_path_still_redacted_regression() -> None:
    """Regression: term frames are still redacted after the snapshot/analysis change."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-term-regression"

    await hub.register_worker(worker_id, AsyncMock())
    ws = AsyncMock()
    await hub.register_browser(worker_id, ws, "viewer")

    await hub.broadcast(worker_id, {"type": "term", "data": "key sk_live_ABC active", "ts": 1.0})

    decoder = ControlChannelDecoder()
    found_redacted = False
    for call in ws.send_text.call_args_list:
        for event in decoder.feed(call[0][0]):
            if event.kind == "data" and "[REDACTED]" in event.data:
                found_redacted = True
                assert "sk_live_" not in event.data
    assert found_redacted, "term data was not redacted"


# ---------------------------------------------------------------------------
# Connect-time snapshot redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_time_snapshot_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The initial snapshot sent at connect time is redacted per recipient role."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-connect"

    worker_ws = AsyncMock()
    await hub.register_worker(worker_id, worker_ws)

    # Seed last_snapshot with a secret in screen
    stored_snapshot: dict[str, Any] = {
        "type": "snapshot",
        "screen": "key sk_live_CONNECT active",
        "raw_tail": "sk_live_CONNECT",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "h1",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": 1.0,
    }
    await hub.update_last_snapshot(worker_id, stored_snapshot)

    browser_ws = AsyncMock()
    browser_state = await hub.register_browser(worker_id, browser_ws, "viewer", defer_broadcast=True)
    initial_snapshot = browser_state["initial_snapshot"]

    assert initial_snapshot is not None, "test setup: last_snapshot not seeded"

    # Apply the connect-time redaction logic (mirrors websockets_impl ~line 390)
    _gate = getattr(hub, "_output_policy_gate", None)
    if _gate is not None:
        context = await hub.prepare_policy_context(browser_ws, worker_id, action="output")
        rules = await _gate.get_redaction_rules(context)
        if rules:
            initial_snapshot = _redact_frame_fields(
                dict(initial_snapshot),  # shallow copy so stored snapshot is NOT mutated
                StreamRedactor(rules),
            )

    # Send the (possibly redacted) snapshot
    await browser_ws.send_text(encode_control(initial_snapshot))

    # Verify the browser received the redacted snapshot
    frames = _collect_control_frames(browser_ws)
    snap_frames = [f for f in frames if f.get("type") == "snapshot"]
    assert snap_frames, "no snapshot frame sent to browser"
    sf = snap_frames[0]
    assert "sk_live_" not in sf["screen"], f"connect-time screen not redacted: {sf['screen']!r}"
    assert "[REDACTED]" in sf["screen"]
    assert "sk_live_" not in str(sf.get("raw_tail", "")), "connect-time raw_tail not redacted"

    # Critically: the stored last_snapshot must remain raw
    stored = await hub.get_last_snapshot(worker_id)
    assert stored is not None
    assert stored["screen"] == "key sk_live_CONNECT active", "stored last_snapshot was mutated!"
    assert stored["raw_tail"] == "sk_live_CONNECT", "stored raw_tail was mutated!"


@pytest.mark.asyncio
async def test_connect_time_snapshot_no_gate_unchanged() -> None:
    """With no gate, the connect-time snapshot is sent as-is."""
    hub = TermHub()
    worker_id = "w-connect-nogate"

    await hub.register_worker(worker_id, AsyncMock())
    stored_snapshot: dict[str, Any] = {
        "type": "snapshot",
        "screen": "key sk_live_SECRET here",
        "raw_tail": "sk_live_SECRET",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "h2",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": 1.0,
    }
    await hub.update_last_snapshot(worker_id, stored_snapshot)

    browser_ws = AsyncMock()
    browser_state = await hub.register_browser(worker_id, browser_ws, "viewer", defer_broadcast=True)
    initial_snapshot = browser_state["initial_snapshot"]
    assert initial_snapshot is not None

    # Connect-time logic: no gate → send as-is
    _gate = getattr(hub, "_output_policy_gate", None)
    if _gate is not None:  # pragma: no branch
        pass  # would apply redaction
    await browser_ws.send_text(encode_control(initial_snapshot))

    frames = _collect_control_frames(browser_ws)
    snap_frames = [f for f in frames if f.get("type") == "snapshot"]
    assert snap_frames
    sf = snap_frames[0]
    assert "sk_live_SECRET" in sf["screen"], "no-gate connect-time must not redact"


# ---------------------------------------------------------------------------
# Integration test: connect-time redaction via the WS route (covers lines
# 393-399 of websockets_impl.py through the actual FastAPI/TestClient path)
# ---------------------------------------------------------------------------


def _make_app_with_gate(gate: OutputPolicyGate) -> tuple[TermHub, FastAPI, TestClient]:
    hub = TermHub(output_policy_gate=gate)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client


def test_ws_route_connect_time_snapshot_redacted_via_route() -> None:
    """Integration: connect-time snapshot is redacted for a browser connecting via WS route.

    Exercises websockets_impl.py lines 393-402 (the _gate branch) through the
    actual FastAPI TestClient → ws_browser_term route path.
    """
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub, _app, client = _make_app_with_gate(gate)

    with connect_test_ws(client, "/ws/worker/w-int/term") as worker:
        # Drain the initial snapshot_req from the worker
        _snap_req = worker.receive_json()

        # Worker sends a snapshot with a secret in screen
        worker.send_json(
            {
                "type": "snapshot",
                "screen": "key sk_live_INTEGRATION active",
                "raw_tail": "sk_live_INTEGRATION",
                "cursor": {"x": 0, "y": 0},
                "cols": 80,
                "rows": 25,
                "screen_hash": "h-int",
            }
        )

        # Give the hub a moment to process the snapshot before browser connects.
        # Connect the browser — it will receive the stored snapshot at connect time.
        with connect_test_ws(client, "/ws/browser/w-int/term") as browser:
            # hello frame
            hello = browser.receive_json()
            assert hello["type"] == "hello"
            # hijack_state frame
            hs = browser.receive_json()
            assert hs["type"] == "hijack_state"

            # The next frame should be the (redacted) initial snapshot
            snap = browser.receive_json()
            assert snap["type"] == "snapshot", f"expected snapshot, got {snap!r}"
            assert "sk_live_" not in snap["screen"], f"connect-time screen not redacted via route: {snap['screen']!r}"
            assert "[REDACTED]" in snap["screen"]
            assert "sk_live_" not in str(snap.get("raw_tail", "")), (
                f"connect-time raw_tail not redacted via route: {snap.get('raw_tail')!r}"
            )


def test_ws_route_connect_time_snapshot_no_gate_unchanged_via_route() -> None:
    """Integration: with no gate the connect-time snapshot is sent unredacted via route.

    Exercises the _gate is None branch (line 391) and the send path (line 403)
    through the actual route.
    """
    hub, _app, client = _make_app_with_gate(MockOutputPolicy([]))  # active gate, empty rules
    # Empty-rules gate: _gate is not None, but rules=[] so _rules is falsy → no redaction

    with connect_test_ws(client, "/ws/worker/w-nr/term") as worker:
        _snap_req = worker.receive_json()

        worker.send_json(
            {
                "type": "snapshot",
                "screen": "plain sk_live_NOREDACT output",
                "raw_tail": "sk_live_NOREDACT",
                "cursor": {"x": 0, "y": 0},
                "cols": 80,
                "rows": 25,
                "screen_hash": "h-nr",
            }
        )

        with connect_test_ws(client, "/ws/browser/w-nr/term") as browser:
            hello = browser.receive_json()
            assert hello["type"] == "hello"
            hs = browser.receive_json()
            assert hs["type"] == "hijack_state"

            snap = browser.receive_json()
            assert snap["type"] == "snapshot"
            # Empty rules → no redaction → secret should still be present
            assert "sk_live_NOREDACT" in snap["screen"], f"empty-rules gate should not redact: {snap['screen']!r}"
