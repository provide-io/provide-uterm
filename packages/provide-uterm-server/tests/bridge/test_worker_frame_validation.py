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
