#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Inbound worker-frame validation (finding 5d).

A malformed worker control frame (e.g. snapshot ``cursor.x="abc"``) raises a
``ValidationError`` inside the frame builder (``make_snapshot_frame`` /
``make_analysis_frame``). Before the fix this hit only the OUTER ``except`` in
the worker WS recv loop, tearing down the worker session *and* every browser
viewing it — a DoS from one bad frame.

The fix is a per-frame ``try/except`` governed by a runtime config flag
``worker_frame_on_invalid``:

- ``"drop"`` (default): drop the bad frame, increment
  ``ws_worker_frame_invalid_total``, log debug, KEEP the session alive.
- ``"reject"``: send a structured error frame, then close 1003 + break.

These tests exercise the drop path (session survives, metric increments,
browser viewer not disconnected), the reject path (1003 close + error frame),
the no-regression hot path, and the config flag's ``Literal`` validation.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hub_with_metrics(**hub_kwargs: Any) -> tuple[TermHub, dict[str, int]]:
    metrics: dict[str, int] = {}

    def on_metric(name: str, value: int = 1) -> None:
        metrics[name] = metrics.get(name, 0) + value

    hub_kwargs.setdefault("resolve_browser_role", lambda _ws, _wid: "operator")
    hub = TermHub(on_metric=on_metric, **hub_kwargs)
    return hub, metrics


def _make_app(hub: TermHub) -> FastAPI:
    app = FastAPI()
    app.include_router(hub.create_router())
    return app


def _read_worker_snapshot_req(worker: Any) -> dict[str, Any]:
    msg = worker.receive_json()
    assert msg["type"] == "snapshot_req"
    return msg


# A snapshot whose ``cursor.x`` is non-coercible to int — ``make_snapshot_frame``
# raises ``ValidationError`` building this. This is the canonical malformed frame.
_BAD_SNAPSHOT = {
    "type": "snapshot",
    "screen": "hi",
    "cursor": {"x": "abc", "y": 0},
    "cols": 80,
    "rows": 25,
    "ts": 1.0,
}
_GOOD_SNAPSHOT = {
    "type": "snapshot",
    "screen": "after-bad",
    "cursor": {"x": 1, "y": 2},
    "cols": 80,
    "rows": 25,
    "ts": 2.0,
}


# ---------------------------------------------------------------------------
# Precondition: the malformed frame really does raise in the builder
# ---------------------------------------------------------------------------


def test_bad_snapshot_raises_in_builder() -> None:
    """Sanity check the test fixture: the bad cursor makes the builder raise."""
    from provide.uterm.server.bridge.frames import make_snapshot_frame

    with pytest.raises(ValidationError):
        make_snapshot_frame(
            screen="hi",
            cursor={"x": "abc", "y": 0},  # type: ignore[dict-item]
            cols=80,
            rows=25,
            screen_hash="",
            cursor_at_end=True,
            has_trailing_space=False,
            prompt_detected=None,
            ts=1.0,
        )


def test_server_snapshot_frame_matches_core_builder() -> None:
    """The server builder delegates to the canonical core builder; this guards
    against the two implementations silently re-diverging on the wire."""
    from provide.uterm.frames import make_snapshot_frame as core_make_snapshot_frame
    from provide.uterm.server.bridge.frames import make_snapshot_frame

    kwargs: dict[str, Any] = {
        "screen": "hello",
        "cursor": {"x": 3, "y": 1},
        "cols": 80,
        "rows": 25,
        "screen_hash": "abc123",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "ts": 123.0,
        "raw_tail": None,
    }
    assert make_snapshot_frame(**kwargs) == core_make_snapshot_frame(**kwargs)


# ---------------------------------------------------------------------------
# Drop path (default) — session survives, metric increments
# ---------------------------------------------------------------------------


def test_drop_keeps_session_alive_and_increments_metric() -> None:
    """Default 'drop': a malformed snapshot is dropped, the metric increments,
    and a SUBSEQUENT valid snapshot still processes (session stays alive)."""
    import asyncio

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        worker.send_json(_BAD_SNAPSHOT)
        # The session must survive the bad frame: a following valid snapshot
        # processes normally and is stored.
        worker.send_json(_GOOD_SNAPSHOT)
        # Poll for the good snapshot to land.
        snap = None
        for _ in range(50):
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None and snap.get("screen") == "after-bad":
                break
            time.sleep(0.02)
        assert snap is not None
        assert snap.get("screen") == "after-bad", "valid frame after a bad one must process"

    assert metrics.get("ws_worker_frame_invalid_total", 0) == 1


def _drain_browser_until(browser: Any, *, ftype: str, screen: str | None = None, limit: int = 40) -> dict[str, Any]:
    """Consume browser frames until one of *ftype* (optionally matching *screen*)
    arrives. Never drains more than the target frame requires, so it cannot
    block waiting for a frame that will not come."""
    for _ in range(limit):
        msg = browser.receive_json()
        if msg.get("type") == ftype and (screen is None or msg.get("screen") == screen):
            return msg
    raise AssertionError(f"frame type={ftype!r} screen={screen!r} not seen within {limit} frames")


def test_drop_does_not_disconnect_browser_viewer() -> None:
    """A browser viewing the worker is NOT torn down when the worker emits a
    malformed frame under the default 'drop' policy."""
    hub, _metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        with connect_test_ws(client, "/ws/browser/w1/term") as browser:
            worker.send_json(_BAD_SNAPSHOT)
            # Send a valid snapshot; the browser must still receive it, proving
            # its connection survived the worker's bad frame. Drain initial
            # frames (hello/hijack_state/...) inline until the target arrives.
            worker.send_json(_GOOD_SNAPSHOT)
            got = _drain_browser_until(browser, ftype="snapshot", screen="after-bad")
            assert got.get("screen") == "after-bad"


def test_drop_handles_bad_prompt_detected_field() -> None:
    """A different malformed snapshot field (``prompt_detected`` not a dict —
    a value that is NOT pre-sanitised by ``_safe_int``/``_safe_float`` and so
    reaches the builder) is also isolated by the per-frame guard."""
    import asyncio

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        # prompt_detected is typed ``dict[str, Any] | None``; a bare string is
        # forwarded straight to make_snapshot_frame (no _safe_* salvage) and
        # raises a ValidationError in the builder.
        worker.send_json(
            {
                "type": "snapshot",
                "screen": "x",
                "cursor": {"x": 0, "y": 0},
                "cols": 80,
                "rows": 25,
                "prompt_detected": "not-a-dict",
                "ts": 1.0,
            }
        )
        worker.send_json(_GOOD_SNAPSHOT)
        snap = None
        for _ in range(50):
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None and snap.get("screen") == "after-bad":
                break
            time.sleep(0.02)
        assert snap is not None and snap.get("screen") == "after-bad"
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 1


# ---------------------------------------------------------------------------
# Reject path — close 1003 + error frame
# ---------------------------------------------------------------------------


def test_reject_closes_with_1003_and_error_frame() -> None:
    """With worker_frame_on_invalid='reject', a malformed frame sends a
    structured error frame then closes the worker WS with code 1003."""
    hub, metrics = _make_hub_with_metrics(worker_frame_on_invalid="reject")
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        worker.send_json(_BAD_SNAPSHOT)
        err = worker.receive_json()
        assert err.get("type") == "error"
        assert err.get("reason") == "invalid_frame"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            worker.receive_json()
        assert excinfo.value.code == 1003
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 1


# ---------------------------------------------------------------------------
# No regression: valid stream + hot path unaffected
# ---------------------------------------------------------------------------


def test_valid_snapshot_stream_processes_unchanged() -> None:
    """A normal valid snapshot processes and never trips the invalid metric."""
    import asyncio

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        worker.send_json(_GOOD_SNAPSHOT)
        snap = None
        for _ in range(50):
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None and snap.get("screen") == "after-bad":
                break
            time.sleep(0.02)
        assert snap is not None and snap.get("screen") == "after-bad"
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 0


def test_term_data_hot_path_unaffected() -> None:
    """Raw terminal data (DataChunk) stays outside the validation wrapper and
    is broadcast to a viewing browser unchanged."""
    from provide.uterm.control_channel import encode_data

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        with connect_test_ws(client, "/ws/browser/w1/term") as browser:
            worker.send_text(encode_data("hello-term"))
            got_term = False
            for _ in range(40):
                msg = browser.receive_json()
                if msg.get("type") == "term" and msg.get("data") == "hello-term":
                    got_term = True
                    break
            assert got_term
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 0


# ---------------------------------------------------------------------------
# Finding 5d-narrowing: a DOWNSTREAM (post-build) failure must NOT be
# mis-isolated as an "invalid worker frame". The per-frame validation guard
# wraps ONLY the frame builder; ``update_last_snapshot`` / ``broadcast`` /
# ``append_event`` run OUTSIDE it so a genuine server-side bug propagates to
# the outer handler instead of being swallowed + miscounted, and so no partial
# state (snapshot stored but never broadcast/recorded) is left behind.
# ---------------------------------------------------------------------------


def test_downstream_broadcast_failure_propagates_not_miscounted() -> None:
    """A snapshot whose BUILDER succeeds but whose ``broadcast`` raises one of
    the caught validation types (here ``ValueError``) must NOT be treated as an
    invalid worker frame.

    Under the OLD shared-try code the downstream ValueError was caught as an
    "invalid frame": ``ws_worker_frame_invalid_total`` incremented and (drop
    policy) the session SURVIVED via ``continue`` — masking a real server-side
    bug. Under the narrowed code the I/O runs outside the builder guard, so the
    exception PROPAGATES to the outer handler and the worker session is torn
    down, and the invalid metric is NOT incremented for it.
    """
    import asyncio

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)

    orig_broadcast = hub.broadcast

    async def _exploding_broadcast(worker_id: str, msg: Any) -> None:
        # Only the snapshot frame's broadcast blows up; the connect-time
        # worker_connected broadcast (and everything else) goes through.
        if isinstance(msg, dict) and msg.get("type") == "snapshot":
            raise ValueError("simulated downstream bug")
        await orig_broadcast(worker_id, msg)

    hub.broadcast = _exploding_broadcast  # type: ignore[method-assign]

    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        worker.send_json(_GOOD_SNAPSHOT)
        # The downstream failure propagates to the outer handler, which tears
        # the worker session down (deregister). Poll the registry for the
        # session to disappear — under the old 'drop+continue' code the worker
        # would still be registered. Bounded so it can never hang.
        torn_down = False
        for _ in range(100):
            if "w1" not in hub.registry:
                torn_down = True
                break
            time.sleep(0.02)
        assert torn_down, "downstream failure must propagate + tear the worker session down, not be swallowed"

    # The crucial assertion: a downstream bug is NOT counted as a bad worker
    # frame. Under the old shared-try code this would have been 1.
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 0
    # Sanity: no lingering snapshot from this torn-down session.
    assert asyncio.run(hub.get_last_snapshot("w1")) is None


def test_builder_failure_leaves_no_partial_state() -> None:
    """When the snapshot BUILDER raises, ``update_last_snapshot`` must NOT have
    run — there is no half-applied state (snapshot stored but never broadcast).
    ``st.last_snapshot`` stays unchanged across the bad frame."""
    import asyncio

    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        # No snapshot has been stored yet.
        assert asyncio.run(hub.get_last_snapshot("w1")) is None
        worker.send_json(_BAD_SNAPSHOT)
        # Drive a follow-up good frame so we can synchronise on the loop having
        # processed the bad one (drop policy keeps the session alive).
        worker.send_json(_GOOD_SNAPSHOT)
        snap = None
        for _ in range(50):
            snap = asyncio.run(hub.get_last_snapshot("w1"))
            if snap is not None and snap.get("screen") == "after-bad":
                break
            time.sleep(0.02)
        # The ONLY snapshot ever stored is the good one — the bad frame never
        # mutated last_snapshot (builder raised before update_last_snapshot).
        assert snap is not None and snap.get("screen") == "after-bad"
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 1


def test_analysis_success_path_broadcasts() -> None:
    """A valid ``analysis`` frame builds and broadcasts to a viewer (success
    path of the analysis branch — I/O runs outside the narrow builder try)."""
    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        with connect_test_ws(client, "/ws/browser/w1/term") as browser:
            worker.send_json({"type": "analysis", "formatted": "analysis-ok", "ts": 1.0})
            got = _drain_browser_until(browser, ftype="analysis")
            assert got.get("formatted") == "analysis-ok"
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 0


def test_status_success_path_broadcasts() -> None:
    """A valid ``status`` frame coerces and broadcasts to a viewer (success
    path of the status branch — I/O runs outside the narrow builder try).

    ``coerce_worker_status_frame`` stamps ``type='status'`` on the broadcast
    frame; the ``worker_status`` name is the append_event event type, not the
    wire frame type.
    """
    hub, metrics = _make_hub_with_metrics()
    app = _make_app(hub)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        connect_test_ws(client, "/ws/worker/w1/term") as worker,
    ):
        _read_worker_snapshot_req(worker)
        with connect_test_ws(client, "/ws/browser/w1/term") as browser:
            worker.send_json({"type": "status", "state": "running", "ts": 1.0})
            got = _drain_browser_until(browser, ftype="status")
            assert got.get("state") == "running"
    assert metrics.get("ws_worker_frame_invalid_total", 0) == 0


# ---------------------------------------------------------------------------
# Config flag validation
# ---------------------------------------------------------------------------


class TestConfigFlag:
    def test_defaults_to_drop(self) -> None:
        from provide.uterm.server.config_schema import UtermServerConfig

        cfg = UtermServerConfig()
        assert cfg.worker_frame_on_invalid == "drop"

    def test_accepts_reject(self) -> None:
        from provide.uterm.server.config_schema import UtermServerConfig

        cfg = UtermServerConfig(worker_frame_on_invalid="reject")
        assert cfg.worker_frame_on_invalid == "reject"

    def test_rejects_other_values(self) -> None:
        from provide.uterm.server.config_schema import UtermServerConfig

        with pytest.raises(ValidationError):
            UtermServerConfig(worker_frame_on_invalid="explode")


# ---------------------------------------------------------------------------
# Factory threading + metric pre-seed
# ---------------------------------------------------------------------------


def test_flag_threaded_to_hub_and_metric_preseeded() -> None:
    """create_server_app threads worker_frame_on_invalid onto the hub and
    pre-seeds ws_worker_frame_invalid_total at 0 in the metrics dict."""
    from provide.uterm.server import create_server_app, default_server_config

    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.worker_frame_on_invalid = "reject"
    app = create_server_app(config)

    metrics: dict[str, int] = app.state.uterm_metrics  # type: ignore[assignment]
    assert "ws_worker_frame_invalid_total" in metrics
    assert metrics["ws_worker_frame_invalid_total"] == 0

    hub = app.state.uterm_hub  # type: ignore[attr-defined]
    assert hub.worker_frame_on_invalid == "reject"
